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
- `ResendRotationAdapter` creates a new Resend API key with an admin key,
  verifies it read-only, stores it, and (with --retire-old) DELETES the old
  key -- Resend has no disable. All HTTP goes through the injected
  `transport` seam.
- `GoogleCloudAPIKeyRotationAdapter` (provider `google-api-key`) rotates a
  Google Cloud API key (`AIza...`, apikeys.googleapis.com) -- a different
  credential type from the GCP SA key above. Copies the old key's
  restrictions, verifies via keys:lookupKey, stores, and (with --retire-old)
  deletes the old key, which Google can undelete for 30 days.

Stub adapters (`VercelRotationAdapter`, `GitHubRotationAdapter`,
`StripeRotationAdapter`) still raise unconditionally. A future real adapter
(Vercel is the confirmed priority target) would authenticate using its OWN
admin credential resolved through `Resolver.resolve_call()` -- see
docs/architecture.md for the worked example.

Manual adapters (`SentryRotationAdapter`) report capability "manual" and
raise with the human re-issue path: the provider has no programmatic
rotation API at all.

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
from typing import Callable, Dict, List, Optional, Tuple

from .atomicio import atomic_write
from .filelock import flock_path
from .paths import home

_ADAPTER_REQUEST_URL = "https://github.com/mdostal/portunus/issues/new?template=adapter-request.yaml"


class RotationAdapterError(RuntimeError):
    """Raised when a rotation adapter cannot rotate a credential."""


@dataclass(frozen=True)
class RotationBinding:
    """Which provider, what account/context, and whether a real adapter
    exists for it yet.

    `status` values:
      "real"   -- a working RotationAdapter exists (capability: auto)
      "stub"   -- adapter registered but unimplemented (capability: unknown)
      "manual" -- no programmatic path exists; human re-issue required (permanent)
    `account` is a free-text, provider-specific context hint (e.g. a Vercel
    team slug, a GitHub org) -- never a credential.
    `superseded_key_ids` carries old key identifiers not yet retired after a
    rotation (populated by the GCP SA adapter when --retire-old is not passed).
    """

    provider: str
    status: str = "stub"
    account: str = ""
    superseded_key_ids: Tuple[str, ...] = field(default_factory=tuple)


def capability_for_status(status: str) -> str:
    """Map RotationBinding.status to audit capability vocabulary.
    "real" -> "auto", "manual" -> "manual", anything else -> "unknown"."""
    if status == "real":
        return "auto"
    if status == "manual":
        return "manual"
    return "unknown"


@dataclass(frozen=True)
class RotationResult:
    """Returned by a successful adapter `.rotate()`. Structurally incapable
    of carrying credential material -- key identifiers and phase metadata
    only, never a secret value."""

    provider: str
    ref_name: str
    # Identifier of the newly-created credential version -- never the value,
    # only an ID the provider assigns (e.g. a GCP key ID, an API key ID).
    key_id: str = ""
    # Phase reached: "refreshed" for OAuth (access token minted, any rotated
    # refresh token persisted back). Future adapters may use "created" /
    # "verified" / "retired".
    phase: str = ""
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
            superseded_key_ids=tuple(cfg.get("superseded_key_ids", [])),
        )
        for provider, cfg in raw.items()
    }


def _rotation_bindings_lock_path(path: Optional[Path] = None) -> Path:
    return _rotation_bindings_path(path).with_suffix(".lock")


def _save_unlocked(bindings: Dict[str, RotationBinding], path: Optional[Path] = None) -> None:
    raw = {
        provider: {
            "status": b.status,
            "account": b.account,
            "superseded_key_ids": list(b.superseded_key_ids),
        }
        for provider, b in bindings.items()
    }
    atomic_write(_rotation_bindings_path(path), json.dumps(raw, indent=2))


def save_rotation_bindings(
    bindings: Dict[str, RotationBinding], path: Optional[Path] = None
) -> None:
    """Persist provider rotation bindings wholesale, 0600 on disk, atomic
    replace under rotation-bindings.lock. Overwrites whatever is on disk --
    a caller doing load/modify/save must use update_rotation_bindings()
    instead, or a concurrent CLI/MCP/UI writer's update can be lost."""
    with flock_path(_rotation_bindings_lock_path(path)):
        _save_unlocked(bindings, path)


def update_rotation_bindings(
    mutate: Callable[[Dict[str, RotationBinding]], None], path: Optional[Path] = None
) -> Dict[str, RotationBinding]:
    """Locked read-modify-write: reload the freshest on-disk bindings under
    rotation-bindings.lock, apply `mutate` (in place), persist, release.
    Same shape as views.py's create/add/remove helpers. Returns the saved
    bindings. `mutate` must not take another Portunus lock (keeps lock
    ordering trivial against backup.snapshot())."""
    with flock_path(_rotation_bindings_lock_path(path)):
        bindings = load_rotation_bindings(path)
        mutate(bindings)
        _save_unlocked(bindings, path)
        return bindings


def rotation_audit_data(registry, rotation_bindings, local_backend=None):
    """Compute rotation audit report: group registry refs and OAuth
    credentials by provider, join each to its rotation capability, and
    collect un-retired superseded key IDs.

    Never includes a credential value. `local_backend` is a
    LocalEncryptedBackend instance (or compatible object exposing
    list_oauth_credentials()). Passing None skips OAuth enumeration.

    Returns a dict with:
      providers: {provider_name: {capability, refs, oauth_accounts,
                                  superseded_key_ids}}
      totals:    {auto, manual, unknown}
      unreadable_oauth_count: int
    """
    # Collect registry refs by provider
    refs_by_provider: Dict[str, List[str]] = {}
    for ref in registry:
        prov = ref.provider or ""
        refs_by_provider.setdefault(prov, []).append(ref.name)

    # Collect OAuth credentials by provider (graceful degradation)
    oauth_by_provider: Dict[str, List[str]] = {}
    unreadable = 0
    if local_backend is not None:
        try:
            for cred in local_backend.list_oauth_credentials():
                ns = cred.get("namespace", {})
                prov = ns.get("provider", "")
                acct = ns.get("account", "")
                oauth_by_provider.setdefault(prov, []).append(acct)
        except Exception:
            unreadable += 1

    # Build the union of all known providers
    all_providers = set(refs_by_provider) | set(oauth_by_provider) | set(rotation_bindings)
    providers_out = {}
    for prov in sorted(all_providers):
        binding = rotation_bindings.get(prov)
        cap = capability_for_status(binding.status) if binding else "unknown"
        superseded = list(binding.superseded_key_ids) if binding else []
        providers_out[prov] = {
            "capability": cap,
            "refs": sorted(refs_by_provider.get(prov, [])),
            "oauth_accounts": sorted(oauth_by_provider.get(prov, [])),
            "superseded_key_ids": superseded,
        }

    totals = {"auto": 0, "manual": 0, "unknown": 0}
    for entry in providers_out.values():
        cap = entry["capability"]
        if cap in totals:
            totals[cap] += len(entry["refs"]) + len(entry["oauth_accounts"])

    return {
        "providers": providers_out,
        "totals": totals,
        "unreadable_oauth_count": unreadable,
    }


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

    def capability(self) -> str:
        return "auto"

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


_RESEND_API = "https://api.resend.com"
# Resend caps API key names at 50 characters.
_RESEND_KEY_NAME_MAX = 50
_RESEND_PERMISSIONS = ("full_access", "sending_access")

# (method, url, headers, json_body_or_None, timeout) -> (status, parsed_json_or_{})
ResendTransport = Callable[[str, str, Dict[str, str], Optional[dict], float], Tuple[int, dict]]


def _default_resend_transport(
    method: str, url: str, headers: Dict[str, str], body: Optional[dict], timeout: float
) -> Tuple[int, dict]:
    return _json_http(method, url, headers, body, timeout, label="Resend API")


def _json_http(
    method: str, url: str, headers: Dict[str, str], body: Optional[dict], timeout: float,
    label: str,
) -> Tuple[int, dict]:
    import urllib.error
    import urllib.request

    data = json.dumps(body).encode() if body is not None else None
    req = urllib.request.Request(url, data=data, headers=dict(headers), method=method)
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            status, raw = resp.status, resp.read()
    except urllib.error.HTTPError as exc:
        status, raw = exc.code, exc.read()
    except urllib.error.URLError as exc:
        raise RotationAdapterError(f"{label} unreachable: {exc.reason}") from exc
    try:
        parsed = json.loads(raw.decode() or "{}")
    except (json.JSONDecodeError, UnicodeDecodeError):
        parsed = {}
    return status, parsed if isinstance(parsed, dict) else {}


class ResendRotationAdapter:
    """Real Resend API key rotation (https://resend.com/docs/api-reference/api-keys).

    Phases (in order):
    1. create  -- ``POST /api-keys`` with the admin key; returns ``{id, token}``
    2. verify  -- ``GET /api-keys?limit=1`` with the NEW key, read-only, sends
                  no email. 200 (full_access key) or 401 ``restricted_api_key``
                  ("restricted to only send emails", i.e. a valid
                  sending_access key) both prove the key is live; anything
                  else (invalid / not active / suspended) fails.
    3. store   -- write the new token to the local backend, then record the
                  new key id in the reference's ``tags["resend_api_key_id"]``
    4. retire  -- OPT-IN ONLY via ``retire_old=True``: ``DELETE
                  /api-keys/{old_id}``. Resend has no disable, so this is a
                  real, irreversible delete -- only pass --retire-old once
                  every consumer has picked up the new key.

    Verify-before-store, same as the GCP adapter: a failed verify stores
    nothing, retires nothing, records ``warn:verify-failed``, and makes a
    best-effort delete of the just-created (never-stored) key so it doesn't
    linger orphaned in the Resend account. Without --retire-old the old key
    id is appended to the ``resend`` RotationBinding's ``superseded_key_ids``
    so ``portunus rotation audit`` shows it as un-retired.

    The ref's tags drive the adapter:
    - ``resend_api_key_id`` -- id of the currently stored key; required for
      --retire-old (Resend tokens don't encode their id)
    - ``resend_admin_ref`` -- name of another reference holding a
      full_access key used for create/delete. Defaults to the ref itself,
      which only works if the rotated key is full_access.
    - ``resend_permission`` -- ``full_access`` (default, matches what the
      existing keys were issued as) or ``sending_access``
    - ``resend_domain_id`` -- optional, restricts a sending_access key

    The admin key is only ever handled inside a ``Resolver.resolve_call()``
    boundary; every HTTP call goes through the injected ``transport`` seam,
    so tests never contact the real API. Neither token is ever returned,
    logged, or put in an error message.
    """

    def __init__(
        self,
        transport: Optional[ResendTransport] = None,
        local_backend=None,
        audit=None,
        registry=None,
        admin_credential_ref: Optional[str] = None,
        timeout: float = 30.0,
    ):
        self._transport = transport or _default_resend_transport
        self._local_backend = local_backend
        self._audit = audit
        # Falls back to resolver.registry when not injected.
        self._registry = registry
        # A "{{secret:NAME}}" template; overrides tags["resend_admin_ref"].
        self._admin_credential_ref = admin_credential_ref
        self._timeout = timeout

    def capability(self) -> str:
        return "auto"

    def rotate(
        self,
        ref,
        resolver=None,
        retire_old: bool = False,
    ) -> RotationResult:
        if hasattr(ref, "sm_name"):
            sm_name = ref.sm_name
            ref_name = getattr(ref, "name", sm_name)
            tags = dict(getattr(ref, "tags", None) or {})
        else:
            sm_name = str(ref)
            ref_name = sm_name
            tags = {}

        if resolver is None:
            raise RotationAdapterError(
                f"ResendRotationAdapter: rotating {ref_name!r} needs a resolver to "
                "resolve the Resend admin key"
            )
        permission = tags.get("resend_permission", "full_access")
        if permission not in _RESEND_PERMISSIONS:
            raise RotationAdapterError(
                f"ResendRotationAdapter: tags['resend_permission'] must be one of "
                f"{_RESEND_PERMISSIONS}, got {permission!r}"
            )
        old_key_id = tags.get("resend_api_key_id", "")
        if retire_old and not old_key_id:
            # Checked before anything is created: refusing up front beats
            # minting a key and then not knowing which one to delete.
            raise RotationAdapterError(
                f"ResendRotationAdapter: --retire-old needs tags['resend_api_key_id'] "
                f"on {ref_name!r} -- the id of the key currently stored"
            )
        admin_template = (
            self._admin_credential_ref
            or "{{secret:%s}}" % tags.get("resend_admin_ref", ref_name)
        )

        local_backend = self._local_backend
        if local_backend is None:
            from .localvault import LocalEncryptedBackend
            local_backend = LocalEncryptedBackend()

        # --- 1. CREATE --------------------------------------------------------
        body = {"name": f"portunus-{ref_name}-{int(time.time())}"[:_RESEND_KEY_NAME_MAX],
                "permission": permission}
        if tags.get("resend_domain_id") and permission == "sending_access":
            body["domain_id"] = tags["resend_domain_id"]
        status, created = self._admin_call(resolver, admin_template, "POST", "/api-keys", body)
        new_key_id = created.get("id", "") if status in (200, 201) else ""
        new_token = created.get("token", "") if status in (200, 201) else ""
        if not new_key_id or not new_token:
            raise RotationAdapterError(
                f"Resend create key failed for {ref_name!r}: {self._describe(status, created)}"
            )

        try:
            # --- 2. VERIFY ----------------------------------------------------
            if not self._verify(new_token):
                if self._audit:
                    self._audit.append("rotate:resend-api-key", sm_name, "warn:verify-failed")
                try:
                    self._admin_call(
                        resolver, admin_template, "DELETE", f"/api-keys/{new_key_id}", None
                    )
                except Exception:
                    pass  # best-effort cleanup; the failure below is what matters
                raise RotationAdapterError(
                    f"Resend key verification failed for {ref_name!r} -- "
                    "rotation aborted; old credential left intact"
                )

            # --- 3. STORE -----------------------------------------------------
            local_backend.store(sm_name, new_token)
        finally:
            del new_token

        self._record_key_id(resolver, ref_name, tags, new_key_id)
        if old_key_id:
            update_rotation_bindings(lambda b: _add_superseded(b, "resend", old_key_id))

        # --- 4. RETIRE (opt-in): DELETE -- Resend has no disable ---------------
        retired = False
        if retire_old:
            status, deleted = self._admin_call(
                resolver, admin_template, "DELETE", f"/api-keys/{old_key_id}", None
            )
            if status not in (200, 204) and status != 404:
                if self._audit:
                    self._audit.append(
                        "rotate:resend-api-key", sm_name, f"warn:retire-failed:new={new_key_id}"
                    )
                raise RotationAdapterError(
                    f"Resend delete of old key {old_key_id} failed for {ref_name!r} "
                    f"({self._describe(status, deleted)}) -- the new key IS stored; "
                    "the old one stays listed as superseded"
                )
            # 404: already gone -- the goal state, so treat it as retired.
            update_rotation_bindings(lambda b: _drop_superseded(b, "resend", old_key_id))
            retired = True

        if self._audit:
            self._audit.append("rotate:resend-api-key", sm_name, f"ok:new={new_key_id}")

        return RotationResult(
            provider="resend",
            ref_name=ref_name,
            key_id=new_key_id,
            phase="stored",
            retired_old=retired,
        )

    # --- HTTP helpers (all calls go through self._transport) -----------------

    def _admin_call(self, resolver, admin_template, method, path, body):
        def _boundary(admin_key: str):
            return self._transport(
                method, _RESEND_API + path, self._headers(admin_key), body, self._timeout
            )

        return resolver.resolve_call(admin_template, _boundary)

    def _verify(self, token: str) -> bool:
        try:
            status, resp = self._transport(
                "GET", _RESEND_API + "/api-keys?limit=1", self._headers(token), None,
                self._timeout,
            )
        except RotationAdapterError:
            return False
        if status == 200:
            return True
        # A sending_access key can't read /api-keys, and Resend says so with
        # 401 restricted_api_key -- which it only returns for a valid key.
        # A key that is "not active" is also restricted_api_key, but 403.
        return status == 401 and resp.get("name") == "restricted_api_key"

    @staticmethod
    def _headers(token: str) -> Dict[str, str]:
        return {
            "Authorization": f"Bearer {token}",
            "Content-Type": "application/json",
            "User-Agent": "portunus-rotation",
        }

    @staticmethod
    def _describe(status: int, resp: dict) -> str:
        # Only Resend's error name/message -- never the response body, which
        # on success carries the new token.
        name = str(resp.get("name", ""))[:60]
        message = str(resp.get("message", ""))[:200]
        return f"HTTP {status}" + (f" {name}: {message}" if name or message else "")

    def _record_key_id(self, resolver, ref_name: str, tags: dict, new_key_id: str) -> None:
        registry = self._registry or getattr(resolver, "registry", None)
        if registry is None or not hasattr(registry, "retag") or ref_name not in registry:
            return
        registry.retag(ref_name, tags={**tags, "resend_api_key_id": new_key_id})


_GOOGLE_APIKEYS_API = "https://apikeys.googleapis.com/v2"
# displayName on a Google Cloud API key is capped at 63 characters.
_GOOGLE_KEY_DISPLAY_NAME_MAX = 63
_GOOGLE_API_KEY_PREFIX = "AIza"
_GOOGLE_API_KEY_LEN = 39


def classify_google_credential(value: str) -> str:
    """Tell a Google Cloud API key from a service-account JSON key by its
    format, never by the reference's name (PANT-1136: `flayr-vertex-ai-*-key`
    could be either).

    Returns "api-key" for an ``AIza...`` key string (39 chars), "service-account"
    for a JSON object with ``type == "service_account"``, else "unknown".
    """
    value = (value or "").strip()
    if value.startswith(_GOOGLE_API_KEY_PREFIX) and len(value) == _GOOGLE_API_KEY_LEN:
        return "api-key"
    if value.startswith("{"):
        try:
            if json.loads(value).get("type") == "service_account":
                return "service-account"
        except (json.JSONDecodeError, AttributeError):
            pass
    return "unknown"


def _default_google_transport(
    method: str, url: str, headers: Dict[str, str], body: Optional[dict], timeout: float
) -> Tuple[int, dict]:
    return _json_http(method, url, headers, body, timeout, label="Google API Keys API")


class GoogleCloudAPIKeyRotationAdapter:
    """Real Google Cloud API key rotation (``apikeys.googleapis.com`` v2,
    https://cloud.google.com/api-keys/docs/reference/rest).

    A different credential type from ``GCPServiceAccountKeyRotationAdapter``:
    an API key (``AIza...``) has no private key and does not impersonate a
    service account, so it gets its own provider name, ``google-api-key``.

    Phases (in order):
    1. find old -- the old key's resource name comes from
                  ``tags["google_api_key_name"]`` or, failing that, from
                  ``keys:lookupKey`` on the stored key string. Its
                  restrictions (API targets, referrer/IP/app limits) and
                  ``serviceAccountEmail`` are copied to the new key, so a
                  rotation never widens what the key can call.
    2. create  -- ``POST projects/{p}/locations/global/keys``, a long-running
                  operation polled to completion, then ``GET .../keyString``
    3. verify  -- ``keys:lookupKey`` on the NEW string must resolve to the new
                  key's name. If ``tags["google_api_key_verify_url"]`` is set,
                  a ``GET`` of that URL with the new key in ``x-goog-api-key``
                  must also return 200 (retried while the key propagates).
    4. store   -- write the new key string to the local backend, then record
                  the new resource name in ``tags["google_api_key_name"]``
    5. retire  -- OPT-IN ONLY via ``retire_old=True``: ``DELETE`` the old key.
                  Google keeps a deleted key for 30 days, during which
                  ``keys.undelete`` restores it.

    Verify-before-store, same as the other real adapters: a failed verify
    stores nothing, retires nothing, records ``warn:verify-failed``, and makes
    a best-effort delete of the just-created key. Without --retire-old the old
    key name is appended to the ``google-api-key`` RotationBinding's
    ``superseded_key_ids``.

    Refuses up front, before any API call, if the stored value is a
    service-account JSON key (rotate that as provider ``gcp``) or anything
    else that is not an ``AIza...`` key string.

    Admin auth is a Google OAuth2 access token (``cloud-platform`` scope) for
    an identity holding ``apikeys.keys.{create,get,getKeyString,lookup,delete}``
    and ``serviceusage.operations.get`` on the project (all in
    ``roles/serviceusage.apiKeysAdmin``), with ``apikeys.googleapis.com`` enabled. It comes from
    ``admin_credential_ref`` / ``tags["google_api_key_admin_ref"]`` (a
    reference resolved through ``Resolver.resolve_call()``), or, if neither is
    set, from ``gcloud auth print-access-token`` through the ``runner`` seam.
    Every HTTP call goes through the injected ``transport`` seam; no key
    string or token is ever returned, logged, or put in an error message.
    """

    def __init__(
        self,
        transport: Optional[ResendTransport] = None,
        runner=None,
        local_backend=None,
        audit=None,
        registry=None,
        admin_credential_ref: Optional[str] = None,
        timeout: float = 30.0,
        sleep: Callable[[float], None] = time.sleep,
        poll_attempts: int = 30,
        poll_interval: float = 2.0,
        verify_attempts: int = 6,
        verify_interval: float = 10.0,
    ):
        self._transport = transport or _default_google_transport
        self._runner = runner or subprocess.run
        self._local_backend = local_backend
        self._audit = audit
        self._registry = registry
        self._admin_credential_ref = admin_credential_ref
        self._timeout = timeout
        self._sleep = sleep
        self._poll_attempts = poll_attempts
        self._poll_interval = poll_interval
        self._verify_attempts = verify_attempts
        self._verify_interval = verify_interval

    def capability(self) -> str:
        return "auto"

    def rotate(
        self,
        ref,
        resolver=None,
        retire_old: bool = False,
    ) -> RotationResult:
        if hasattr(ref, "sm_name"):
            sm_name = ref.sm_name
            ref_name = getattr(ref, "name", sm_name)
            tags = dict(getattr(ref, "tags", None) or {})
            project = getattr(ref, "project", "") or ""
        else:
            sm_name = str(ref)
            ref_name = sm_name
            tags = {}
            project = ""

        local_backend = self._local_backend
        if local_backend is None:
            from .localvault import LocalEncryptedBackend
            local_backend = LocalEncryptedBackend()

        try:
            current = local_backend.access(sm_name, project=project)
        except Exception:
            current = None
        old_name = tags.get("google_api_key_name", "")
        if current is not None:
            kind = classify_google_credential(current)
            if kind != "api-key":
                hint = (
                    "it holds a service-account JSON key -- rotate it as provider 'gcp'"
                    if kind == "service-account"
                    else "it does not look like a Google Cloud API key (AIza...)"
                )
                raise RotationAdapterError(
                    f"GoogleCloudAPIKeyRotationAdapter: refusing to rotate {ref_name!r}: {hint}"
                )
        elif not old_name:
            raise RotationAdapterError(
                f"GoogleCloudAPIKeyRotationAdapter: {ref_name!r} has no stored value and no "
                "tags['google_api_key_name'] -- cannot tell which key to replace"
            )

        admin_template = self._admin_credential_ref or (
            "{{secret:%s}}" % tags["google_api_key_admin_ref"]
            if tags.get("google_api_key_admin_ref") else None
        )
        if admin_template and resolver is None:
            raise RotationAdapterError(
                f"GoogleCloudAPIKeyRotationAdapter: rotating {ref_name!r} needs a resolver "
                "to resolve the admin credential"
            )

        def call(method, url, body=None):
            return self._admin_call(resolver, admin_template, method, url, body)

        # --- 1. FIND OLD ------------------------------------------------------
        if not old_name:
            status, found = call("GET", self._lookup_url(current))
            old_name = found.get("name", "") if status == 200 else ""
            if not old_name:
                raise RotationAdapterError(
                    f"Google API key lookup failed for {ref_name!r}: "
                    f"{self._describe(status, found)} -- set tags['google_api_key_name']"
                )
        del current
        status, old_key = call("GET", f"{_GOOGLE_APIKEYS_API}/{old_name}")
        if status != 200:
            raise RotationAdapterError(
                f"Google API key get {old_name} failed for {ref_name!r}: "
                f"{self._describe(status, old_key)}"
            )
        parent = old_name.split("/locations/")[0]

        # --- 2. CREATE --------------------------------------------------------
        body = {"displayName": f"portunus-{ref_name}-{int(time.time())}"[:_GOOGLE_KEY_DISPLAY_NAME_MAX]}
        if old_key.get("restrictions"):
            body["restrictions"] = old_key["restrictions"]
        if old_key.get("serviceAccountEmail"):
            body["serviceAccountEmail"] = old_key["serviceAccountEmail"]
        status, op = call("POST", f"{_GOOGLE_APIKEYS_API}/{parent}/locations/global/keys", body)
        if status != 200:
            raise RotationAdapterError(
                f"Google API key create failed for {ref_name!r}: {self._describe(status, op)}"
            )
        new_name = self._wait_operation(call, op, f"create for {ref_name!r}").get("name", "")
        if not new_name:
            raise RotationAdapterError(
                f"Google API key create for {ref_name!r} returned no key name"
            )
        new_key_id = new_name.rsplit("/", 1)[-1]

        new_key: Optional[str] = None
        try:
            status, ks = call("GET", f"{_GOOGLE_APIKEYS_API}/{new_name}/keyString")
            new_key = ks.get("keyString", "") if status == 200 else ""

            # --- 3. VERIFY ----------------------------------------------------
            if not new_key or not self._verify(call, new_key, new_name, tags):
                if self._audit:
                    self._audit.append("rotate:google-api-key", sm_name, "warn:verify-failed")
                try:
                    call("DELETE", f"{_GOOGLE_APIKEYS_API}/{new_name}")
                except Exception:
                    pass  # best-effort cleanup; the failure below is what matters
                raise RotationAdapterError(
                    f"Google API key verification failed for {ref_name!r} -- "
                    "rotation aborted; old credential left intact"
                )

            # --- 4. STORE -----------------------------------------------------
            local_backend.store(sm_name, new_key)
        finally:
            del new_key

        self._record_key_name(resolver, ref_name, tags, new_name)
        update_rotation_bindings(lambda b: _add_superseded(b, "google-api-key", old_name))

        # --- 5. RETIRE (opt-in): DELETE, undeletable for 30 days ---------------
        retired = False
        if retire_old:
            status, op = call("DELETE", f"{_GOOGLE_APIKEYS_API}/{old_name}")
            if status not in (200, 404):
                if self._audit:
                    self._audit.append(
                        "rotate:google-api-key", sm_name, f"warn:retire-failed:new={new_key_id}"
                    )
                raise RotationAdapterError(
                    f"Google API key delete of {old_name} failed for {ref_name!r} "
                    f"({self._describe(status, op)}) -- the new key IS stored; "
                    "the old one stays listed as superseded"
                )
            update_rotation_bindings(lambda b: _drop_superseded(b, "google-api-key", old_name))
            retired = True

        if self._audit:
            self._audit.append("rotate:google-api-key", sm_name, f"ok:new={new_key_id}")

        return RotationResult(
            provider="google-api-key",
            ref_name=ref_name,
            key_id=new_key_id,
            phase="stored",
            retired_old=retired,
        )

    # --- HTTP helpers (all calls go through self._transport) -----------------

    @staticmethod
    def _lookup_url(key_string: str) -> str:
        from urllib.parse import quote
        return f"{_GOOGLE_APIKEYS_API}/keys:lookupKey?keyString={quote(key_string, safe='')}"

    def _admin_call(self, resolver, admin_template, method, url, body):
        def _boundary(token: str):
            headers = {
                "Authorization": f"Bearer {token}",
                "Content-Type": "application/json",
                "User-Agent": "portunus-rotation",
            }
            return self._transport(method, url, headers, body, self._timeout)

        if admin_template:
            return resolver.resolve_call(admin_template, _boundary)
        proc = self._runner(
            ["gcloud", "auth", "print-access-token"], capture_output=True, text=True, timeout=60
        )
        if proc.returncode != 0:
            raise RotationAdapterError(
                f"gcloud auth print-access-token failed: {proc.stderr.strip()[:200]}"
            )
        return _boundary(proc.stdout.strip())

    def _wait_operation(self, call, op: dict, context: str) -> dict:
        for _ in range(self._poll_attempts):
            if op.get("error"):
                err = op["error"]
                raise RotationAdapterError(
                    f"Google API key {context} failed: "
                    f"{str(err.get('message', ''))[:200] if isinstance(err, dict) else ''}"
                )
            if op.get("done"):
                return op.get("response") or {}
            self._sleep(self._poll_interval)
            status, op = call("GET", f"{_GOOGLE_APIKEYS_API}/{op.get('name', '')}")
            if status != 200:
                raise RotationAdapterError(
                    f"Google API key {context}: operation poll failed: {self._describe(status, op)}"
                )
        raise RotationAdapterError(f"Google API key {context}: operation did not finish in time")

    def _verify(self, call, new_key: str, new_name: str, tags: dict) -> bool:
        try:
            status, found = call("GET", self._lookup_url(new_key))
        except RotationAdapterError:
            return False
        if status != 200 or found.get("name") != new_name:
            return False
        probe_url = tags.get("google_api_key_verify_url", "")
        if not probe_url:
            return True
        # A new key can take a few minutes to start working -- retry.
        for attempt in range(self._verify_attempts):
            if attempt:
                self._sleep(self._verify_interval)
            try:
                status, _ = self._transport(
                    "GET", probe_url,
                    {"x-goog-api-key": new_key, "User-Agent": "portunus-rotation"},
                    None, self._timeout,
                )
            except RotationAdapterError:
                continue
            if status == 200:
                return True
        return False

    @staticmethod
    def _describe(status: int, resp: dict) -> str:
        # Only Google's error status/message -- never a success body, which
        # for getKeyString carries the key.
        err = resp.get("error") if isinstance(resp, dict) else None
        if not isinstance(err, dict):
            return f"HTTP {status}"
        return f"HTTP {status} {str(err.get('status', ''))[:60]}: {str(err.get('message', ''))[:200]}"

    def _record_key_name(self, resolver, ref_name: str, tags: dict, new_name: str) -> None:
        registry = self._registry or getattr(resolver, "registry", None)
        if registry is None or not hasattr(registry, "retag") or ref_name not in registry:
            return
        registry.retag(ref_name, tags={**tags, "google_api_key_name": new_name})


def _add_superseded(bindings: Dict[str, RotationBinding], provider: str, key_id: str) -> None:
    b = bindings.get(provider) or RotationBinding(provider=provider, status="real")
    if key_id not in b.superseded_key_ids:
        b = RotationBinding(
            provider=b.provider, status=b.status, account=b.account,
            superseded_key_ids=b.superseded_key_ids + (key_id,),
        )
    bindings[provider] = b


def _drop_superseded(bindings: Dict[str, RotationBinding], provider: str, key_id: str) -> None:
    b = bindings.get(provider)
    if b is None:
        return
    bindings[provider] = RotationBinding(
        provider=b.provider, status=b.status, account=b.account,
        superseded_key_ids=tuple(k for k in b.superseded_key_ids if k != key_id),
    )


class VercelRotationAdapter:
    """Vercel -- STUB. No real calls.

    Confirmed by the user as the priority target for the first REAL
    rotation adapter, but not built real this epic ("not going to care too
    much for first version"). Once real, `.rotate()` would resolve its own
    admin token via `resolver.resolve_call("{{secret:portunus-admin-vercel-
    token}}", ...)` -- never hardcoded here, never handled outside the
    normal boundary-only sinks.
    """

    def capability(self) -> str:
        return "unknown"

    def rotate(self, ref, resolver=None, retire_old: bool = False) -> None:
        raise RotationAdapterError(
            f"Vercel rotation is not yet implemented -- request it: {_ADAPTER_REQUEST_URL}"
        )


class GitHubRotationAdapter:
    """GitHub -- STUB. No real calls."""

    def capability(self) -> str:
        return "unknown"

    def rotate(self, ref, resolver=None, retire_old: bool = False) -> None:
        raise RotationAdapterError(
            f"GitHub rotation is not yet implemented -- request it: {_ADAPTER_REQUEST_URL}"
        )


class StripeRotationAdapter:
    """Stripe -- STUB. No real calls."""

    def capability(self) -> str:
        return "unknown"

    def rotate(self, ref, resolver=None, retire_old: bool = False) -> None:
        raise RotationAdapterError(
            f"Stripe rotation is not yet implemented -- request it: {_ADAPTER_REQUEST_URL}"
        )


class SentryRotationAdapter:
    """Sentry -- MANUAL. No real calls, and none are possible.

    Every Sentry token-management endpoint -- user auth tokens
    (``/api/0/api-tokens/``), organization auth tokens
    (``/api/0/organizations/{org}/org-auth-tokens/``) and internal-integration
    tokens (``/api/0/sentry-apps/{slug}/api-tokens/``) -- uses
    ``SessionNoAuthTokenAuthentication``, which rejects any request carrying
    an ``Authorization`` header, and is marked ``ApiPublishStatus.PRIVATE``.
    A bearer admin token, whatever its scopes, cannot mint or revoke another
    token, so there is no create -> verify -> retire path to drive. Session-
    cookie scripting of the web UI is deliberately not attempted (SSO/2FA,
    undocumented, breaks silently). See docs/provider-rotation-matrix.md.

    Registered so `rotation run` explains the human re-issue path instead of
    reporting "no adapter registered".
    """

    def capability(self) -> str:
        return "manual"

    def rotate(self, ref, resolver=None, retire_old: bool = False) -> None:
        raise RotationAdapterError(
            "Sentry tokens cannot be rotated programmatically -- Sentry's token "
            "endpoints accept session auth only and reject bearer tokens. Re-issue by "
            "hand (Settings > Auth Tokens), store the new value, then revoke the old one "
            "in the same page; see docs/provider-rotation-matrix.md (Sentry auth tokens)."
        )


_ADAPTERS = {
    "oauth": OAuthRefreshRotationAdapter,
    "gcp": GCPServiceAccountKeyRotationAdapter,
    "google-api-key": GoogleCloudAPIKeyRotationAdapter,
    "resend": ResendRotationAdapter,
    "vercel": VercelRotationAdapter,
    "github": GitHubRotationAdapter,
    "stripe": StripeRotationAdapter,
    "sentry": SentryRotationAdapter,
}


def rotation_adapter_for(provider: str):
    """Resolve a provider name to its RotationAdapter instance, or None if
    no adapter (real or stub) is registered for it yet."""
    adapter_cls = _ADAPTERS.get(provider)
    return adapter_cls() if adapter_cls else None


def audit_rotate(audit, ref_name: str, result: str) -> dict:
    """Append a 'rotate' audit entry.

    result encodes the phase reached: ``ok:created`` / ``ok:verified`` /
    ``ok:retired`` / ``warn:verify-failed`` / ``err:<reason>``. Always call
    this even for failures -- a half-rotated credential is exactly the state
    a human needs the trail for. ``AuditChain.verify()`` is unaffected: the
    action field is free-form and the hash chain covers it byte-for-byte.
    """
    return audit.append("rotate", ref_name, result)


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
