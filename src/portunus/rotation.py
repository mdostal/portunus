"""Rotation provenance -- which provider a reference came from, what
account/context a rotation would run against, and whether Portunus has a
real adapter for it yet.

Mirrors ARCA's own real/stub posture (backend.py) applied to a genuinely
different kind of provider integration: not "where does the value live"
but "who would rotate it, and how." `RotationBinding` is the direct analog
of `VaultBinding` -- keyed by provider (not per-reference, same per-project
reasoning), persisted as PORTUNUS_HOME/rotation-bindings.json, 0600,
atomic-replace.

Real adapters:
- `OAuthRefreshRotationAdapter` delegates to the existing
  `OAuthBackend`/`OAuthRefreshTokenAuth` path -- a single interface drives
  every stored OAuth refresh credential without a per-provider job.
- `GCPServiceAccountKeyRotationAdapter` mints a new GCP SA key, verifies it,
  stores it to the local backend, and (with --retire-old) disables the
  superseded key. All gcloud calls go through the injected `runner` seam.
  Deletion of the old key is never performed in the same call as disable --
  that is left to a future grace-period sweep.

Stub adapters (`VercelRotationAdapter`, `GitHubRotationAdapter`,
`StripeRotationAdapter`) still raise unconditionally. A future real adapter
(Vercel is the confirmed priority target) would authenticate using its OWN
admin credential resolved through `Resolver.resolve_call()` -- see
docs/architecture.md for the worked example.

See also: docs/rotation.md for the provider capability matrix and the
non-destructive-default rationale.
"""
from __future__ import annotations

import json
import os
import subprocess
import tempfile
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Dict, List, Optional

from .paths import home

_ADAPTER_REQUEST_URL = "https://github.com/mdostal/portunus/issues/new?template=adapter-request.yaml"


class RotationAdapterError(RuntimeError):
    """Raised when a rotation adapter cannot rotate a credential."""


@dataclass(frozen=True)
class RotationBinding:
    """Which provider, what account/context, and whether a real adapter
    exists for it yet. `status` mirrors ARCA's own "real" | "stub"
    language. `account` is a free-text, provider-specific context hint
    (e.g. a Vercel team slug, a GitHub org) -- never a credential."""

    provider: str
    status: str = "stub"
    account: str = ""


@dataclass(frozen=True)
class RotationResult:
    """Returned by a successful adapter `.rotate()`. Structurally incapable
    of carrying credential material -- key identifiers and phase metadata
    only, never a secret value."""

    provider: str
    ref_name: str
    # Phase reached: "refreshed" for OAuth (access token minted, any rotated
    # refresh token persisted back). Future adapters may use "created" /
    # "verified" / "retired".
    phase: str
    retired_old: bool = False
    timestamp: int = field(default_factory=lambda: int(time.time()))


def _rotation_bindings_path(path: Optional[Path] = None) -> Path:
    return path or (home() / "rotation-bindings.json")


def load_rotation_bindings(path: Optional[Path] = None) -> Dict[str, RotationBinding]:
    """Load PORTUNUS_HOME/rotation-bindings.json (provider -> RotationBinding).
    Missing file means no bindings configured yet -- returns {}, matching
    load_vault_bindings' empty-state handling."""
    bindings_path = _rotation_bindings_path(path)
    if not bindings_path.exists():
        return {}
    raw = json.loads(bindings_path.read_text() or "{}")
    return {
        provider: RotationBinding(
            provider=provider,
            status=cfg.get("status", "stub"),
            account=cfg.get("account", ""),
        )
        for provider, cfg in raw.items()
    }


def save_rotation_bindings(
    bindings: Dict[str, RotationBinding], path: Optional[Path] = None
) -> None:
    """Persist provider rotation bindings, 0600 on disk, atomic replace --
    same idiom save_vault_bindings uses."""
    bindings_path = _rotation_bindings_path(path)
    bindings_path.parent.mkdir(parents=True, exist_ok=True)
    raw = {
        provider: {"status": b.status, "account": b.account}
        for provider, b in bindings.items()
    }
    tmp = bindings_path.with_suffix(".json.tmp")
    tmp.write_text(json.dumps(raw, indent=2))
    os.chmod(tmp, 0o600)
    os.replace(tmp, bindings_path)
    os.chmod(bindings_path, 0o600)


class OAuthRefreshRotationAdapter:
    """Real OAuth refresh token rotation -- wraps the existing
    `OAuthBackend`/`OAuthRefreshTokenAuth` path rather than implementing a
    second refresh grant.

    Semantic distinction from static-key adapters (Vercel, GitHub, Stripe):
    OAuth refresh "rotation" replaces a short-lived access token the
    *provider itself* rotates on every use. There is no old key to retire,
    so `retire_old=True` is semantically invalid and is rejected rather than
    silently ignored.

    The real work -- the RFC 6749 refresh_token grant, and persisting a
    provider-rotated refresh_token back to the vault immediately -- lives
    in `OAuthBackend.access()`. One call drives all three: mint, detect,
    persist. Any additional stored OAuth credential is served through the
    same adapter instance with no new per-provider job.
    """

    def __init__(self, local_backend=None, audit=None, transport=None):
        self._local_backend = local_backend
        self._audit = audit
        self._transport = transport

    def _make_backend(self):
        from .backend import OAuthBackend
        return OAuthBackend(
            local_backend=self._local_backend,
            audit=self._audit,
            transport=self._transport,
        )

    def rotate(
        self,
        ref,
        resolver=None,
        retire_old: bool = False,
    ) -> RotationResult:
        if retire_old:
            raise RotationAdapterError(
                "OAuth refresh rotation has no superseded credential to retire -- "
                "the provider replaces the spent refresh token as part of the grant "
                "itself; --retire-old is not applicable here."
            )
        # `ref` is either a Reference-like object with a .sm_name attribute
        # (provider:account) or a plain "provider:account" string, for
        # direct/test callers.
        if hasattr(ref, "sm_name"):
            sm_name = ref.sm_name
            ref_name = getattr(ref, "name", sm_name)
        else:
            sm_name = str(ref)
            ref_name = sm_name

        if ":" not in sm_name:
            raise RotationAdapterError(
                f"OAuthRefreshRotationAdapter: sm_name must be 'provider:account', got {sm_name!r}"
            )
        provider = sm_name.split(":", 1)[0]

        backend = self._make_backend()
        try:
            # access() mints a fresh access token via OAuthRefreshTokenAuth
            # and, if the provider returned a rotated refresh_token, persists
            # it back via store_oauth_credential -- no second implementation
            # of the refresh flow lives here.
            backend.access(sm_name)
        except Exception as exc:
            raise RotationAdapterError(
                f"OAuth refresh rotation failed for {sm_name}: {exc}"
            ) from exc

        return RotationResult(
            provider=provider,
            ref_name=ref_name,
            phase="refreshed",
            retired_old=False,
        )


class GCPServiceAccountKeyRotationAdapter:
    """Real GCP service-account key rotation.

    Phases (in order):
    1. create  -- mint a new key via ``gcloud iam service-accounts keys create``
    2. verify  -- confirm the new key works before touching stored state
    3. store   -- write the new key material to the local backend
    4. retire  -- OPT-IN ONLY via ``retire_old=True``: DISABLE the superseded
                  key. Deletion is NEVER performed in the same call -- a
                  disabled key can be re-enabled, a deleted one cannot be
                  recovered. Deletion is deferred to a future grace-period sweep.

    Verify-before-store is non-negotiable: if verify fails, nothing is stored
    and nothing is retired; the audit log records ``warn:verify-failed`` and
    ``RotationAdapterError`` is raised, leaving the pre-rotation state intact.

    All gcloud calls go through the injected ``runner`` seam (same pattern as
    ``GcloudBackend``), so tests never contact a real GCP API.

    The ref must expose:
    - ``sm_name`` -- the secret name in the local backend
    - ``tags["iam_account"]`` -- the service account email
      (e.g. ``sa@project.iam.gserviceaccount.com``)
    - ``project`` -- the GCP project (optional; passed to gcloud if set)

    If a resolver is supplied, the adapter resolves its own admin credential
    via ``Resolver.resolve_call()`` through the normal boundary-only path
    before each gcloud invocation. Without a resolver the runner is called
    directly (tests supply a fake runner that needs no real credential).
    """

    def __init__(self, runner=None, local_backend=None, audit=None, admin_credential_ref=None):
        self._runner = runner or subprocess.run
        self._local_backend = local_backend
        self._audit = audit
        # When set, the adapter resolves its own admin GCP credential via
        # Resolver.resolve_call() before each gcloud invocation, passing the
        # minted token as --access-token-file. Leave None in tests (the fake
        # runner needs no real credential); set to a "{{secret:...}}" placeholder
        # in production so the normal boundary-only path applies.
        self._admin_credential_ref = admin_credential_ref

    def capability(self) -> str:
        return "auto"

    def rotate(
        self,
        ref,
        resolver=None,
        retire_old: bool = False,
    ) -> RotationResult:
        # --- unpack ref -------------------------------------------------------
        if hasattr(ref, "sm_name"):
            sm_name = ref.sm_name
            ref_name = getattr(ref, "name", sm_name)
            iam_account = (getattr(ref, "tags", None) or {}).get("iam_account", "")
            project = getattr(ref, "project", "") or ""
        else:
            sm_name = str(ref)
            ref_name = sm_name
            iam_account = ""
            project = ""

        if not iam_account:
            raise RotationAdapterError(
                f"GCPServiceAccountKeyRotationAdapter: ref {ref_name!r} has no "
                "iam_account -- set tags['iam_account'] to the service account email"
            )

        local_backend = self._local_backend
        if local_backend is None:
            from .localvault import LocalEncryptedBackend
            local_backend = LocalEncryptedBackend()

        # --- capture old key id before anything changes -----------------------
        old_key_id: Optional[str] = None
        if retire_old:
            old_key_id = self._current_key_id(local_backend, sm_name, project)

        # --- 1. CREATE --------------------------------------------------------
        fd, key_path = tempfile.mkstemp(prefix="portunus-sa-key-", suffix=".json")
        os.fchmod(fd, 0o600)
        os.close(fd)
        new_key_id: Optional[str] = None
        new_key_content: Optional[str] = None
        try:
            self._gcloud_create_key(iam_account, project, key_path, resolver)
            raw = Path(key_path).read_text()
            try:
                new_key_id = json.loads(raw).get("private_key_id", "")
            except (json.JSONDecodeError, AttributeError):
                new_key_id = ""
            new_key_content = raw

            # --- 2. VERIFY ----------------------------------------------------
            verify_ok = self._gcloud_verify_key(key_path, project, resolver)
            if not verify_ok:
                if self._audit:
                    self._audit.append("rotate:gcp-sa-key", sm_name, "warn:verify-failed")
                raise RotationAdapterError(
                    f"GCP SA key verification failed for {ref_name!r} -- "
                    "rotation aborted; old credential left intact"
                )

            # --- 3. STORE -----------------------------------------------------
            local_backend.store(sm_name, new_key_content)

        finally:
            # Key material must not linger on disk longer than necessary.
            try:
                os.unlink(key_path)
            except OSError:
                pass

        # --- 4. RETIRE (opt-in): DISABLE only, never delete -------------------
        if retire_old and old_key_id:
            self._gcloud_disable_key(iam_account, project, old_key_id, resolver)

        if self._audit:
            result_tag = f"ok:new={new_key_id}" if new_key_id else "ok"
            self._audit.append("rotate:gcp-sa-key", sm_name, result_tag)

        return RotationResult(
            provider="gcp",
            ref_name=ref_name,
            phase="stored",
            retired_old=bool(retire_old and old_key_id),
        )

    # --- gcloud helpers (all calls go through self._runner) ------------------

    def _gcloud_create_key(self, iam_account: str, project: str, output_path: str, resolver) -> None:
        cmd = [
            "gcloud", "iam", "service-accounts", "keys", "create",
            output_path,
            f"--iam-account={iam_account}",
            "--format=json",
        ]
        if project:
            cmd.append(f"--project={project}")
        self._run(cmd, resolver, context=f"create key for {iam_account}")

    def _gcloud_verify_key(self, key_path: str, project: str, resolver) -> bool:
        activate_cmd = [
            "gcloud", "auth", "activate-service-account",
            f"--key-file={key_path}",
        ]
        try:
            self._run(activate_cmd, resolver, context="activate new key")
        except RotationAdapterError:
            return False

        token_cmd = ["gcloud", "auth", "print-access-token"]
        if project:
            token_cmd.append(f"--project={project}")
        try:
            self._run(token_cmd, resolver, context="print-access-token")
        except RotationAdapterError:
            return False
        return True

    def _gcloud_disable_key(self, iam_account: str, project: str, key_id: str, resolver) -> None:
        cmd = [
            "gcloud", "iam", "service-accounts", "keys", "disable",
            key_id,
            f"--iam-account={iam_account}",
        ]
        if project:
            cmd.append(f"--project={project}")
        self._run(cmd, resolver, context=f"disable key {key_id}")

    def _run(self, cmd: list, resolver, context: str) -> None:
        """Execute a gcloud command through the injected runner.

        When an admin_credential_ref was configured AND a resolver is available,
        resolves the adapter's own GCP admin credential and passes it via
        --access-token-file (same boundary-only pattern as GcloudBackend).
        Otherwise the command runs as-is -- tests inject a fake runner that
        needs no real credential and set admin_credential_ref=None.
        """
        if self._admin_credential_ref and resolver is not None:
            import os as _os
            import tempfile as _tmp

            def _run_with_token(token: str) -> None:
                fd, tf = _tmp.mkstemp(prefix="portunus-rotation-token-")
                _os.fchmod(fd, 0o600)
                try:
                    with _os.fdopen(fd, "w") as fh:
                        fh.write(token)
                    augmented = [cmd[0], f"--access-token-file={tf}"] + cmd[1:]
                    proc = self._runner(augmented, capture_output=True, text=True, timeout=60)
                    if proc.returncode != 0:
                        raise RotationAdapterError(
                            f"gcloud {context} failed: {proc.stderr.strip()[:200]}"
                        )
                finally:
                    try:
                        _os.unlink(tf)
                    except OSError:
                        pass

            resolver.resolve_call(self._admin_credential_ref, _run_with_token)
        else:
            proc = self._runner(cmd, capture_output=True, text=True, timeout=60)
            if proc.returncode != 0:
                raise RotationAdapterError(
                    f"gcloud {context} failed: {proc.stderr.strip()[:200]}"
                )

    def _current_key_id(self, local_backend, sm_name: str, project: str) -> Optional[str]:
        """Read the currently stored SA key JSON and extract private_key_id."""
        try:
            current = local_backend.access(sm_name, project=project)
            return json.loads(current).get("private_key_id") or None
        except Exception:
            return None


class VercelRotationAdapter:
    """Vercel -- STUB. No real calls.

    Confirmed by the user as the priority target for the first REAL
    rotation adapter, but not built real this epic ("not going to care too
    much for first version"). Once real, `.rotate()` would resolve its own
    admin token via `resolver.resolve_call("{{secret:portunus-admin-vercel-
    token}}", ...)` -- never hardcoded here, never handled outside the
    normal boundary-only sinks.
    """

    def rotate(self, ref, resolver=None) -> None:
        raise RotationAdapterError(
            f"Vercel rotation is not yet implemented -- request it: {_ADAPTER_REQUEST_URL}"
        )


class GitHubRotationAdapter:
    """GitHub -- STUB. No real calls."""

    def rotate(self, ref, resolver=None) -> None:
        raise RotationAdapterError(
            f"GitHub rotation is not yet implemented -- request it: {_ADAPTER_REQUEST_URL}"
        )


class StripeRotationAdapter:
    """Stripe -- STUB. No real calls."""

    def rotate(self, ref, resolver=None) -> None:
        raise RotationAdapterError(
            f"Stripe rotation is not yet implemented -- request it: {_ADAPTER_REQUEST_URL}"
        )


_ADAPTERS = {
    "oauth": OAuthRefreshRotationAdapter,
    "gcp": GCPServiceAccountKeyRotationAdapter,
    "vercel": VercelRotationAdapter,
    "github": GitHubRotationAdapter,
    "stripe": StripeRotationAdapter,
}


def rotation_adapter_for(provider: str):
    """Resolve a provider name to its RotationAdapter instance, or None if
    no adapter (real or stub) is registered for it yet."""
    adapter_cls = _ADAPTERS.get(provider)
    return adapter_cls() if adapter_cls else None


def run_periodic_oauth_refresh(
    local_backend=None,
    audit=None,
    transport=None,
) -> List[RotationResult]:
    """Drive every stored OAuth credential through OAuthRefreshRotationAdapter.

    One job, one adapter, all credentials -- adding a new refresh-token
    credential requires no new per-provider job. Failures are collected and
    re-raised after all credentials are attempted so a single bad entry
    doesn't stop the rest.
    """
    if local_backend is None:
        from .localvault import LocalEncryptedBackend
        local_backend = LocalEncryptedBackend()

    credentials = local_backend.list_oauth_credentials()
    adapter = OAuthRefreshRotationAdapter(
        local_backend=local_backend, audit=audit, transport=transport
    )

    results: List[RotationResult] = []
    errors: List[str] = []
    for cred in credentials:
        ns = cred.get("namespace", {})
        provider = ns.get("provider", "")
        account = ns.get("account", "")
        sm_name = f"{provider}:{account}"
        try:
            result = adapter.rotate(sm_name)
            results.append(result)
        except RotationAdapterError as exc:
            errors.append(str(exc))

    if errors:
        raise RotationAdapterError(
            f"periodic OAuth refresh failed for {len(errors)} credential(s): "
            + "; ".join(errors)
        )
    return results
