"""Roll a credential everywhere it is used: `portunus rotation roll` (GH #230).

`portunus rotation run` rotates a credential at its provider and stores the
new value in the vault. Every place that holds a copy (a Convex deployment's
env, a Vercel project's env, a GCP Secret Manager secret) still has the old
value until someone pushes it there by hand. This module does the whole job
in one ordered step:

1. rotate   -- the provider adapter creates the new key, verifies it against
               the provider and stores it in the local vault
2. push     -- write the new value to every target in the reference's
               ``rotation_targets`` tag, then fire the Vercel deploy hook so
               the new env is picked up
3. verify   -- read every target that can be read back and check it holds
               the new value, then run each ``rotation_verify`` check (a
               Convex function that exercises the key, e.g. a test send)
4. retire   -- delete the old key at the provider (skip with ``keep_old``)

If push or verify fails, everything is put back: the targets already written
get the old value again, the vault gets the old value, the reference's key-id
tag is restored, the new key is deleted at the provider and the deploy hook
fires again. The old key is only deleted once every consumer is verified.

Target specs (``rotation_targets`` tag, space separated, same style as
``parity_targets``):

- ``convex:<deploy-key-ref>[:<VAR>]`` -- env var VAR on the Convex deployment
  that deploy key belongs to (Deployment Platform API). VAR defaults to the
  reference's single ``injected_as`` name. Read back for verify.
- ``vercel:<token-ref>:<project>[:<VAR>]`` -- Vercel project env var, upserted
  as an encrypted var for the ``vercel_env_target`` tag's environment
  (default ``production``) under the optional ``vercel_team_id`` tag. Vercel
  doesn't hand an encrypted value back, so this target is not read back.
  The ``vercel_deploy_hook_ref`` tag names a reference holding a deploy hook
  URL, POSTed once after the push (and again after a rollback).
- ``gcp:<project>[:<secret>]`` -- a new Secret Manager version; the secret
  defaults to the reference's sm_name. Read back for verify.

Verify specs (``rotation_verify`` tag):

- ``convex-fn:<deploy-key-ref>:<module:function>`` -- run that Convex function
  with the deploy key (admin auth) and the ``rotation_verify_args`` tag (a
  JSON object) as its args. Passes when Convex reports success and, if the
  function returns an object with a ``status`` field, that field equals the
  ``rotation_verify_expect`` tag (default ``sent``).

Secret boundary: the old value is only ever held inside a
``Resolver.resolve_call()`` boundary and the new one inside a local-vault
read in this module. Neither is returned, logged, printed or put in an error
message; targets compare values with ``hmac.compare_digest`` and report a
bool. Every network call goes through an injected transport so tests never
reach a real API.
"""
from __future__ import annotations

import hmac
import json
import re
import types
from dataclasses import dataclass, field
from typing import Callable, Dict, List, Optional, Tuple
from urllib.parse import quote

from .rotation import RotationAdapterError, _json_http

TARGETS_TAG = "rotation_targets"
VERIFY_TAG = "rotation_verify"
VERIFY_ARGS_TAG = "rotation_verify_args"
VERIFY_EXPECT_TAG = "rotation_verify_expect"
VERCEL_HOOK_TAG = "vercel_deploy_hook_ref"
_SPEC_SEP = re.compile(r"[\s,;]+")

_VERCEL_API = "https://api.vercel.com"

# (method, url, headers, json_body_or_None, timeout) -> (status, parsed_json_or_{})
Transport = Callable[[str, str, Dict[str, str], Optional[dict], float], Tuple[int, dict]]


class RolloutError(RotationAdapterError):
    """A roll failed. The message says what was rolled back; never a value."""


def _default_transport(method, url, headers, body, timeout):
    return _json_http(method, url, headers, body, timeout, "rollout target")


def _specs(ref, tag: str) -> List[str]:
    raw = (getattr(ref, "tags", None) or {}).get(tag, "")
    return [s for s in _SPEC_SEP.split(raw) if s]


def _single_injected_var(ref) -> str:
    names = list((getattr(ref, "injected_as", None) or {}).keys())
    return names[0] if len(names) == 1 else ""


def _same(a: str, b: str) -> bool:
    return hmac.compare_digest(a.encode(), b.encode())


# ---------------------------------------------------------------------------
# Targets
# ---------------------------------------------------------------------------

class ConvexEnvTarget:
    """Env var on a Convex deployment, via the Deployment Platform API with
    ``Authorization: Convex <deploy key>`` (the same auth liveness uses)."""

    readable = True

    def __init__(self, spec, resolver, key_ref, var, transport, timeout=30.0):
        self.spec = spec
        self._resolver = resolver
        self._key_ref = key_ref
        self._var = var
        self._send = transport
        self._timeout = timeout

    def _with_key(self, fn):
        from .liveness import CONVEX_URL_TAG, convex_deployment_url

        override = (getattr(self._key_ref, "tags", None) or {}).get(CONVEX_URL_TAG, "")

        def boundary(key: str):
            base = convex_deployment_url(key, override)
            if not base:
                raise RolloutError(f"{self.spec}: deploy key names no deployment and no "
                                   f"{CONVEX_URL_TAG!r} tag")
            return fn(base, {"Authorization": f"Convex {key.strip()}",
                             "Content-Type": "application/json"})

        return self._resolver.resolve_call("{{secret:%s}}" % self._key_ref.name, boundary)

    def write(self, value: str) -> None:
        def call(base, headers):
            status, _ = self._send(
                "POST", base + "/api/v1/update_environment_variables", headers,
                {"changes": [{"name": self._var, "value": value}]}, self._timeout,
            )
            if status not in (200, 204):
                raise RolloutError(f"{self.spec}: update_environment_variables -> HTTP {status}")

        self._with_key(call)

    def holds(self, value: str) -> bool:
        def call(base, headers):
            status, body = self._send(
                "GET", base + "/api/v1/list_environment_variables", headers, None, self._timeout,
            )
            if status != 200:
                raise RolloutError(f"{self.spec}: list_environment_variables -> HTTP {status}")
            env = body.get("environmentVariables")
            current = env.get(self._var) if isinstance(env, dict) else None
            del body, env
            return isinstance(current, str) and _same(current, value)

        return bool(self._with_key(call))


class VercelEnvTarget:
    """Vercel project env var, upserted as ``encrypted``. Write-only."""

    readable = False

    def __init__(self, spec, resolver, token_ref, project, var, env_target, team_id,
                 transport, timeout=30.0):
        self.spec = spec
        self._resolver = resolver
        self._token_ref = token_ref
        self._project = project
        self._var = var
        self._env_target = env_target
        self._team_id = team_id
        self._send = transport
        self._timeout = timeout

    def write(self, value: str) -> None:
        url = f"{_VERCEL_API}/v10/projects/{quote(self._project, safe='')}/env?upsert=true"
        if self._team_id:
            url += "&teamId=" + quote(self._team_id, safe="")
        body = {"key": self._var, "value": value, "type": "encrypted",
                "target": [self._env_target]}

        def boundary(token: str):
            return self._send("POST", url, {"Authorization": f"Bearer {token}",
                                            "Content-Type": "application/json"},
                              body, self._timeout)

        status, _ = self._resolver.resolve_call("{{secret:%s}}" % self._token_ref.name, boundary)
        if status not in (200, 201):
            raise RolloutError(f"{self.spec}: Vercel env upsert -> HTTP {status}")

    def holds(self, value: str) -> Optional[bool]:
        return None


class GcpSecretTarget:
    """A new GCP Secret Manager version, through the project's vault binding
    (WIF, --account, or ambient gcloud) exactly like `portunus gcp add-version`."""

    readable = True

    def __init__(self, spec, backend, project, secret):
        self.spec = spec
        self._backend = backend
        self._project = project
        self._secret = secret

    def write(self, value: str) -> None:
        from .backend import BackendError
        try:
            self._backend.add_version(self._secret, value, project=self._project)
        except BackendError as exc:
            raise RolloutError(f"{self.spec}: {exc}") from None

    def holds(self, value: str) -> bool:
        from .backend import BackendError
        try:
            current = self._backend.access(self._secret, project=self._project)
        except BackendError as exc:
            raise RolloutError(f"{self.spec}: {exc}") from None
        try:
            return _same(current, value)
        finally:
            del current


class ConvexFunctionCheck:
    """Run a Convex function as admin and judge its result. The function is
    what exercises the new key (e.g. sends a test email through Resend)."""

    def __init__(self, spec, resolver, key_ref, path, args, expect, transport, timeout=60.0):
        self.spec = spec
        self._env = ConvexEnvTarget(spec, resolver, key_ref, "", transport, timeout)
        self._path = path
        self._args = args
        self._expect = expect
        self._send = transport
        self._timeout = timeout

    def run(self) -> None:
        def call(base, headers):
            status, body = self._send(
                "POST", base + "/api/function", headers,
                {"path": self._path, "args": [self._args], "format": "convex_encoded_json"},
                self._timeout,
            )
            if status != 200 or body.get("status") != "success":
                # Convex's errorMessage is the function's own text; keep it
                # short and never include the response value.
                msg = str(body.get("errorMessage", ""))[:200]
                raise RolloutError(f"{self.spec}: HTTP {status}" + (f" {msg}" if msg else ""))
            returned = body.get("value")
            if isinstance(returned, dict) and "status" in returned \
                    and returned["status"] != self._expect:
                raise RolloutError(
                    f"{self.spec}: returned status {str(returned['status'])[:40]!r}, "
                    f"expected {self._expect!r}"
                )

        self._env._with_key(call)


class DeployHook:
    """POST a Vercel deploy hook URL held in a reference, to redeploy."""

    def __init__(self, resolver, hook_ref_name, transport, timeout=30.0):
        self.name = hook_ref_name
        self._resolver = resolver
        self._send = transport
        self._timeout = timeout

    def fire(self) -> None:
        def boundary(url: str):
            if not url.strip().startswith("https://"):
                raise RolloutError(f"{self.name}: deploy hook is not an https URL")
            return self._send("POST", url.strip(), {}, None, self._timeout)

        status, _ = self._resolver.resolve_call("{{secret:%s}}" % self.name, boundary)
        if status not in (200, 201):
            raise RolloutError(f"deploy hook {self.name}: HTTP {status}")


# ---------------------------------------------------------------------------
# Plan
# ---------------------------------------------------------------------------

@dataclass
class RollPlan:
    targets: List[object] = field(default_factory=list)
    checks: List[object] = field(default_factory=list)
    hooks: List[DeployHook] = field(default_factory=list)


def build_plan(ref, registry, resolver, transport: Optional[Transport] = None,
               gcp_backend_for: Optional[Callable[[str], object]] = None,
               skip: Tuple[str, ...] = ()) -> RollPlan:
    """Parse the reference's target and verify specs. Raises RolloutError on
    a malformed spec or a missing auth reference -- before anything runs."""
    send = transport or _default_transport
    tags = dict(getattr(ref, "tags", None) or {})
    plan = RollPlan()

    def need_ref(spec, name, what):
        found = registry.get(name) if name else None
        if found is None:
            raise RolloutError(f"{spec!r}: no {what} reference {name!r} in registry")
        return found

    for spec in _specs(ref, TARGETS_TAG):
        if spec in skip:
            continue
        kind, _, rest = spec.partition(":")
        if kind == "convex":
            key_name, _, var = rest.partition(":")
            key_ref = need_ref(spec, key_name, "deploy-key")
            var = var or _single_injected_var(ref)
            if not var:
                raise RolloutError(f"{spec!r}: name the env var (convex:<deploy-key-ref>:<VAR>)")
            plan.targets.append(ConvexEnvTarget(spec, resolver, key_ref, var, send))
        elif kind == "vercel":
            parts = rest.split(":")
            if len(parts) not in (2, 3) or not all(parts):
                raise RolloutError(f"{spec!r}: expected vercel:<token-ref>:<project>[:<VAR>]")
            token_ref = need_ref(spec, parts[0], "Vercel token")
            var = parts[2] if len(parts) == 3 else _single_injected_var(ref)
            if not var:
                raise RolloutError(f"{spec!r}: name the env var (vercel:<token-ref>:<project>:<VAR>)")
            plan.targets.append(VercelEnvTarget(
                spec, resolver, token_ref, parts[1], var,
                tags.get("vercel_env_target", "production"), tags.get("vercel_team_id", ""), send,
            ))
        elif kind == "gcp":
            project, _, secret = rest.partition(":")
            if not project:
                raise RolloutError(f"{spec!r}: expected gcp:<project>[:<secret>]")
            backend = (gcp_backend_for or _default_gcp_backend)(project)
            plan.targets.append(GcpSecretTarget(spec, backend, project, secret or ref.sm_name))
        else:
            raise RolloutError(f"{spec!r}: unknown target kind {kind!r}")

    for spec in _specs(ref, VERIFY_TAG):
        kind, _, rest = spec.partition(":")
        if kind != "convex-fn":
            raise RolloutError(f"{spec!r}: unknown verify kind {kind!r}")
        key_name, _, path = rest.partition(":")
        key_ref = need_ref(spec, key_name, "deploy-key")
        if not path:
            raise RolloutError(f"{spec!r}: expected convex-fn:<deploy-key-ref>:<module:function>")
        try:
            args = json.loads(tags.get(VERIFY_ARGS_TAG, "") or "{}")
        except ValueError:
            raise RolloutError(f"{VERIFY_ARGS_TAG} tag is not valid JSON") from None
        if not isinstance(args, dict):
            raise RolloutError(f"{VERIFY_ARGS_TAG} tag must be a JSON object")
        plan.checks.append(ConvexFunctionCheck(
            spec, resolver, key_ref, path, args, tags.get(VERIFY_EXPECT_TAG, "sent"), send,
        ))

    hook_name = tags.get(VERCEL_HOOK_TAG, "")
    if hook_name and any(isinstance(t, VercelEnvTarget) for t in plan.targets):
        need_ref(VERCEL_HOOK_TAG, hook_name, "deploy hook")
        plan.hooks.append(DeployHook(resolver, hook_name, send))
    return plan


def _default_gcp_backend(project: str):
    from .audit import AuditChain
    from .backend import GcloudBackend, load_vault_bindings
    return GcloudBackend(project=project, bindings=load_vault_bindings(), audit=AuditChain())


# ---------------------------------------------------------------------------
# Roll
# ---------------------------------------------------------------------------

@dataclass
class RollResult:
    provider: str
    ref_name: str
    key_id: str
    written: List[str]
    verified: List[str]
    unverifiable: List[str]
    retired_old: bool
    retire_error: str = ""


def _snapshot(ref):
    """The reference's identity and tags as they are now -- rotate() retags
    the live object, and a rollback needs the pre-rotation tags."""
    return types.SimpleNamespace(
        name=ref.name, sm_name=ref.sm_name, project=getattr(ref, "project", ""),
        tags=dict(getattr(ref, "tags", None) or {}),
        injected_as=dict(getattr(ref, "injected_as", None) or {}),
    )


def roll_everywhere(ref, adapter, plan: RollPlan, resolver, local_backend, audit=None,
                    provider: str = "", keep_old: bool = False) -> RollResult:
    """Rotate `ref` with `adapter`, push the new value to every target in
    `plan`, verify, then delete the old key. On a push or verify failure,
    roll everything back and raise RolloutError.

    `adapter` must support rotate(), old_key_id(), delete_key() and
    restore_key_id() -- see ResendRotationAdapter."""
    before = _snapshot(ref)
    old_key_id = adapter.old_key_id(before)
    if not keep_old and not old_key_id:
        raise RolloutError(
            f"{ref.name!r} records no current key id, so the old key can't be deleted "
            "afterwards -- set it, or pass --keep-old"
        )

    def flow(old_value: str) -> RollResult:
        result = adapter.rotate(ref, resolver=resolver, retire_old=False)
        written: List[object] = []
        verified: List[str] = []
        try:
            def push(new_value: str) -> None:
                for target in plan.targets:
                    target.write(new_value)
                    written.append(target)
                for hook in plan.hooks:
                    hook.fire()
                for target in plan.targets:
                    if target.readable:
                        if not target.holds(new_value):
                            raise RolloutError(f"{target.spec}: does not hold the new value")
                        verified.append(target.spec)

            _with_local(local_backend, before.sm_name, push)
            for check in plan.checks:
                check.run()
                verified.append(check.spec)
        except Exception as exc:
            reason = str(exc) if isinstance(exc, RotationAdapterError) else type(exc).__name__
            problems = _rollback(before, adapter, plan, written, resolver, local_backend,
                                 old_value, result.key_id)
            if audit is not None:
                audit.append(f"roll:{provider}", before.sm_name,
                             "rolled-back" + (":incomplete" if problems else ""))
            msg = (f"roll of {before.name!r} failed -- {reason}. Rolled back: "
                   f"{len(written)} target(s) restored, vault restored, new key "
                   f"{result.key_id} deleted")
            if problems:
                msg += ". ROLLBACK INCOMPLETE, fix by hand: " + "; ".join(problems)
            raise RolloutError(msg) from None

        retired, retire_error = False, ""
        if not keep_old:
            try:
                retired = adapter.delete_key(before, resolver, old_key_id)
                if retired:
                    adapter.mark_retired(old_key_id)
                else:
                    retire_error = f"delete of old key {old_key_id} was refused"
            except Exception as exc:
                retire_error = (str(exc) if isinstance(exc, RotationAdapterError)
                                else type(exc).__name__)
        if audit is not None:
            audit.append(f"roll:{provider}", before.sm_name,
                         f"ok:new={result.key_id}:targets={len(written)}"
                         + (":old-deleted" if retired else ":old-kept"))
        return RollResult(
            provider=provider, ref_name=before.name, key_id=result.key_id,
            written=[t.spec for t in written], verified=verified,
            unverifiable=[t.spec for t in plan.targets if not t.readable],
            retired_old=retired, retire_error=retire_error,
        )

    return resolver.resolve_call("{{secret:%s}}" % before.name, flow)


def _with_local(local_backend, sm_name: str, fn) -> None:
    """Hand the value the adapter just stored to `fn`, and nowhere else."""
    value = local_backend.access(sm_name)
    try:
        fn(value)
    finally:
        del value


def _rollback(before, adapter, plan, written, resolver, local_backend, old_value,
              new_key_id) -> List[str]:
    """Best-effort undo, every step attempted. Returns what could not be
    undone (spec names and error text, never a value)."""
    problems: List[str] = []

    def attempt(label, fn):
        try:
            fn()
        except Exception as exc:
            detail = str(exc) if isinstance(exc, RotationAdapterError) else type(exc).__name__
            problems.append(f"{label} ({detail})")

    for target in reversed(written):
        attempt(target.spec, lambda t=target: t.write(old_value))
    if written:
        for hook in plan.hooks:
            attempt(f"deploy hook {hook.name}", hook.fire)
    attempt("vault", lambda: local_backend.store(before.sm_name, old_value))
    attempt("key-id tag", lambda: adapter.restore_key_id(before, resolver))

    def delete_new():
        if not adapter.delete_key(before, resolver, new_key_id):
            raise RolloutError("refused")

    attempt(f"delete new key {new_key_id}", delete_new)
    return problems


def _resend_factory(local_backend, audit):
    from .rotation import ResendRotationAdapter
    return ResendRotationAdapter(local_backend=local_backend, audit=audit)


# Providers whose adapter supports the rollback hooks roll_everywhere needs.
ROLLABLE: Dict[str, Callable] = {"resend": _resend_factory}


def describe_plan(ref, plan: RollPlan, keep_old: bool) -> List[str]:
    """Human-readable dry-run lines: spec names only, never a value."""
    lines = [f"roll {ref.name}  provider={ref.provider}"]
    lines.append("  1. rotate at the provider, verify the new key, store it in the vault")
    if plan.targets:
        lines.append("  2. push to:")
        for t in plan.targets:
            lines.append(f"       {t.spec}" + ("" if t.readable else "  (write-only, not read back)"))
    else:
        lines.append(f"  2. push to: nothing -- no {TARGETS_TAG!r} tag")
    for h in plan.hooks:
        lines.append(f"     then redeploy via {h.name}")
    lines.append("  3. verify: read back every readable target"
                 + "".join(f"; {c.spec}" for c in plan.checks))
    lines.append("  4. " + ("keep the old key (--keep-old)" if keep_old
                            else "delete the old key at the provider"))
    lines.append("  any failure in 2-3 rolls everything back and deletes the new key")
    return lines
