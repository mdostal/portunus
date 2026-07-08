"""The resolver — boundary-only ``{{secret:NAME}}`` substitution.

This is the heart of Portunus and the whole reason it exists. The LLM/agent
only ever emits a placeholder: ``{{secret:shared-anthropic}}``. The real value
is fetched from Secret Manager and substituted ONLY at the execution boundary
(the outbound API/tool/build call), which happens *after* the model produced
its output.

Non-negotiable invariants (see docs/harness-side-injection.md):
  * a resolved value is NEVER returned up the stack to a caller/model
  * a resolved value is NEVER written to the audit log or any normal-mode disk
  * the only sinks for a resolved value are: a caller-supplied boundary
    callable, a 0600 temp file the caller must delete, or the argv of an
    exec'd subprocess

Every placeholder passes ``Broker.check_injectable`` before its value is
fetched, so lifecycle/approval policy is enforced at the point of use.
"""
from __future__ import annotations

import os
import re
import stat
import tempfile
from typing import Callable, List, Optional, Sequence

from .backend import SecretBackend
from .broker import Broker

# {{secret:NAME}} where NAME is anything but a closing brace.
PLACEHOLDER_RE = re.compile(r"\{\{secret:([^}]+)\}\}")


class UnknownReference(KeyError):
    """A placeholder names a reference that is not in the registry."""


class Resolver:
    def __init__(self, registry, backend: SecretBackend, broker: Broker):
        self.registry = registry
        self.backend = backend
        self.broker = broker

    # --- introspection (safe: names only, never values) ------------------
    @staticmethod
    def find_refs(text: str) -> List[str]:
        """Return the unique, sorted reference names in `text`."""
        return sorted(set(PLACEHOLDER_RE.findall(text)))

    def _fetch(self, name: str) -> str:
        """Policy-gate then fetch one value. Caller must not leak the result."""
        if name not in self.registry:
            raise UnknownReference(name)
        ref = self.broker.check_injectable(name)   # raises on dropped/revoked/gated
        value = self.backend.access(ref.sm_name)    # the only place a value appears
        self.broker.audit.append("resolve", ref.sm_name, "ok")
        return value

    def _substitute(self, text: str) -> str:
        """Return `text` with every placeholder replaced by its live value.

        Private on purpose: the plaintext result must only flow into a boundary
        sink, never be handed back to arbitrary caller code.
        """
        cache: dict[str, str] = {}

        def repl(match: "re.Match[str]") -> str:
            name = match.group(1)
            if name not in cache:
                cache[name] = self._fetch(name)
            return cache[name]

        return PLACEHOLDER_RE.sub(repl, text)

    # --- boundary sinks --------------------------------------------------
    def resolve_call(self, template: str, boundary: Callable[[str], object]) -> object:
        """Resolve `template` and hand the plaintext ONLY to `boundary`.

        Returns whatever `boundary` returns (e.g. an HTTP response). The
        resolved text is scrubbed from the local frame and never returned.
        """
        resolved = self._substitute(template)
        try:
            return boundary(resolved)
        finally:
            del resolved  # drop our reference promptly

    def resolve_exec(
        self,
        argv: Sequence[str],
        runner: Optional[Callable[[Sequence[str]], object]] = None,
    ) -> object:
        """Resolve placeholders inside argv, then exec (default) or run.

        Plaintext exists only in the argv of the replacement process. With the
        default runner this call does not return (``os.execvp`` replaces the
        process); tests pass a fake runner that captures argv.
        """
        if not argv:
            raise ValueError("resolve_exec requires a non-empty argv")
        resolved = [self._substitute(arg) for arg in argv]
        if runner is not None:
            return runner(resolved)
        os.execvp(resolved[0], resolved)  # noqa: does not return

    def resolve_to_tempfile(self, text: str) -> str:
        """Resolve `text` into a fresh 0600 temp file; return its path.

        The path — never the value — is returned. The caller is responsible for
        reading and deleting the file. Mirrors ``secrets resolve`` text mode.
        """
        fd, path = tempfile.mkstemp(prefix="portunus-")
        try:
            os.fchmod(fd, stat.S_IRUSR | stat.S_IWUSR)  # 0600
            with os.fdopen(fd, "w") as fh:
                fh.write(self._substitute(text))
        except BaseException:
            os.unlink(path)
            raise
        return path
