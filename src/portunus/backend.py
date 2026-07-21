"""ARCA — the vault store: Secret Manager backends behind one tiny interface.

ARCA (Roman strongbox/coffer) is the store itself — the GCP Secret Manager
tier here, plus the local-encrypted tier (DOS-448).

A backend answers exactly one dangerous question — "give me the plaintext for
this SM name" — and is called ONLY from the resolver, at the boundary. Keeping
it behind a tiny interface means tests use an in-memory ``MockBackend`` and
never touch GCP, while production uses the local encrypted vault by default.

Two seams, on purpose:

  * ``SecretBackend``  — the READ seam OSTIARIUS resolves through. One method
    (``access``). Anything that can answer it can serve placeholders.
  * ``ArcaBackend``    — the full VAULT seam (access / set / list_names /
    delete). This is the plug-and-play contract every ARCA tier must satisfy
    so that future cloud adapters (GCP SM, AWS SM, Vault, ...) slot in behind
    the same facade without touching OSTIARIUS, Petitio, or the CLIs.

Build order is LOCAL-FIRST (canonical): ``LocalVault`` is the default and the
only complete tier today. ``GcloudBackend`` keeps read-only parity with
``bin/secrets`` but its write-side seam methods fail closed until the cloud
adapter slice lands.
"""
from __future__ import annotations

import shutil
import subprocess
from typing import Dict, List, Protocol, runtime_checkable


class BackendError(RuntimeError):
    """Raised when a backend cannot return a value (missing / access denied)."""


@runtime_checkable
class SecretBackend(Protocol):
    def access(self, sm_name: str) -> str:
        """Return the latest plaintext for `sm_name`, or raise BackendError."""
        ...


@runtime_checkable
class ArcaBackend(SecretBackend, Protocol):
    """The full ARCA vault seam — what any pluggable tier must implement.

    Contract notes:
      * ``access`` is the ONLY method that may return a plaintext value, and
        it must only be called from the OSTIARIUS boundary path.
      * ``set`` never returns the value; ``list_names`` and ``delete`` handle
        names/existence only — no method other than ``access`` may expose,
        log, or echo secret material.
    """

    def set(self, sm_name: str, value: str) -> None:
        """Store `value` as the newest version of `sm_name`."""
        ...

    def list_names(self) -> List[str]:
        """Return the sorted secret names present in this tier (never values)."""
        ...

    def delete(self, sm_name: str) -> bool:
        """Remove `sm_name` entirely. True if it existed, False otherwise."""
        ...


class MockBackend:
    """In-memory ARCA tier for tests and dry runs. Never touches the network."""

    def __init__(self, values: Dict[str, str] | None = None):
        self._values = dict(values or {})

    def set(self, sm_name: str, value: str) -> None:
        self._values[sm_name] = value

    def access(self, sm_name: str) -> str:
        try:
            return self._values[sm_name]
        except KeyError as exc:
            raise BackendError(f"unknown secret: {sm_name}") from exc

    def list_names(self) -> List[str]:
        return sorted(self._values)

    def delete(self, sm_name: str) -> bool:
        return self._values.pop(sm_name, None) is not None


class GcloudBackend:
    """GCP Secret Manager via the gcloud CLI (matches bin/secrets exactly).

    Read-only today: the full cloud ArcaBackend adapter is a LATER slice —
    Portunus is local-first by canon. The write-side seam methods exist so the
    seam is visible, but they fail closed.
    """

    _NOT_YET = (
        "GcloudBackend is read-only: the cloud ARCA adapter is a later slice "
        "(Portunus is local-first). Use the local vault (default)."
    )

    def __init__(self, project: str = "", timeout: float = 30.0):
        self.project = project
        self.timeout = timeout

    def access(self, sm_name: str) -> str:
        if shutil.which("gcloud") is None:
            raise BackendError("gcloud CLI not found on PATH")
        cmd = ["gcloud", "secrets", "versions", "access", "latest", f"--secret={sm_name}"]
        if self.project:
            cmd.append(f"--project={self.project}")
        try:
            proc = subprocess.run(
                cmd, capture_output=True, text=True, timeout=self.timeout,
            )
        except subprocess.TimeoutExpired as exc:
            raise BackendError(f"gcloud timeout for {sm_name}") from exc
        if proc.returncode != 0:
            # stderr may name the secret but never contains the value.
            raise BackendError(
                f"gcloud access failed for {sm_name}: {proc.stderr.strip()[:200]}"
            )
        return proc.stdout

    def set(self, sm_name: str, value: str) -> None:
        raise BackendError(self._NOT_YET)

    def list_names(self) -> List[str]:
        raise BackendError(self._NOT_YET)

    def delete(self, sm_name: str) -> bool:
        raise BackendError(self._NOT_YET)
