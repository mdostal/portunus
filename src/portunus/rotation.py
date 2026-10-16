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

- `GitHubRotationAdapter` resets OAuth App tokens the operator's own OAuth
  App issued; every other GitHub credential type is manual or not
  applicable, decided per reference (see the GitHub table below).

Stub adapters (`VercelRotationAdapter`, `StripeRotationAdapter`) still
raise unconditionally. A future real adapter
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


# --- GitHub ---------------------------------------------------------------
#
# GitHub has no single rotation story -- it depends entirely on which kind of
# token is stored (researched 2026-10-16 against docs.github.com/en/rest,
# PANT-1141 / portunus#188; see docs/rotation.md's GitHub section):
#
#   classic PAT (ghp_)            manual -- no create/reissue API; UI only
#   fine-grained PAT (github_pat_) manual -- the org API can only *revoke*
#                                  access, never create or reissue
#   OAuth App token (gho_)        auto, ONLY when the operator owns the OAuth
#                                  App (holds its client_secret): PATCH
#                                  /applications/{client_id}/token resets it
#                                  without user involvement. A `gh auth login`
#                                  token is gho_ too, but belongs to GitHub
#                                  CLI's OAuth App -- nobody but GitHub holds
#                                  that client_secret, so it is manual.
#   GitHub App user token (ghu_)  not applicable here -- expiring user tokens
#                                  come with a ghr_ refresh token; store the
#                                  bundle via `portunus oauth store` and the
#                                  OAuth refresh adapter rotates it.
#   installation token (ghs_)     not applicable -- GitHub expires it after
#                                  1 hour; re-mint from the App's private key.
#   GitHub App private key (PEM)  manual -- generate/delete is UI-only.

GITHUB_API = "https://api.github.com"
_GITHUB_API_VERSION = "2022-11-28"

# token-prefix -> subtype. Order matters: "github_pat_" before "gh?_" checks.
_GITHUB_PREFIXES = (
    ("github_pat_", "fine_grained_pat"),
    ("ghp_", "classic_pat"),
    ("gho_", "oauth_app"),
    ("ghu_", "app_user"),
    ("ghs_", "app_installation"),
    ("ghr_", "app_refresh"),
    ("-----BEGIN", "app_private_key"),
)

GITHUB_SUBTYPE_CAPABILITY = {
    "classic_pat": "manual",
    "fine_grained_pat": "manual",
    "oauth_app": "auto",
    "app_user": "not_applicable",
    "app_refresh": "not_applicable",
    "app_installation": "not_applicable",
    "app_private_key": "manual",
}

_GITHUB_SUBTYPE_REASON = {
    "classic_pat": (
        "classic personal access tokens (ghp_) have no create/reissue API -- GitHub "
        "only issues them through its web UI. Generate a new token in Settings -> "
        "Developer settings, re-store it, then revoke the old one."
    ),
    "fine_grained_pat": (
        "fine-grained personal access tokens (github_pat_) have no create/reissue "
        "API -- the org endpoints can only revoke access. Generate a new token in "
        "Settings -> Developer settings, re-store it, then revoke the old one."
    ),
    "app_user": (
        "GitHub App user tokens (ghu_) expire and are refreshed with their ghr_ "
        "refresh token -- store the bundle via `portunus oauth store` and the "
        "OAuth refresh adapter rotates it; there is nothing for this adapter to do."
    ),
    "app_refresh": (
        "a GitHub App refresh token (ghr_) is rotated by the OAuth refresh grant "
        "itself -- store it via `portunus oauth store`, not as a plain reference."
    ),
    "app_installation": (
        "GitHub App installation tokens (ghs_) expire after 1 hour and are "
        "re-minted from the App's private key -- there is nothing to rotate."
    ),
    "app_private_key": (
        "GitHub App private keys can only be generated/deleted in the App's "
        "settings page -- no API exists. Generate a new key, re-store it, then "
        "delete the old one."
    ),
}


def github_token_subtype(value: str = "", tags: Optional[dict] = None) -> str:
    """Classify a GitHub credential. An explicit `github_token_type` tag wins
    (it is metadata, readable without touching the value); otherwise the
    token's own documented prefix decides. Returns "" when neither says."""
    tagged = (tags or {}).get("github_token_type", "")
    if tagged:
        return tagged
    stripped = (value or "").strip()
    for prefix, subtype in _GITHUB_PREFIXES:
        if stripped.startswith(prefix):
            return subtype
    return ""


def _github_http(method: str, url: str, headers: dict, body: Optional[dict], timeout: float):
    """Default HTTP seam: returns (status, parsed_json_or_{}). Never raises on
    an HTTP error status -- the caller decides what a 401/404 means. Tests
    inject a fake with the same signature."""
    import urllib.error
    import urllib.request

    data = json.dumps(body).encode() if body is not None else None
    req = urllib.request.Request(url, data=data, headers=headers, method=method)
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            status, raw = resp.status, resp.read()
    except urllib.error.HTTPError as exc:
        status, raw = exc.code, exc.read()
    try:
        parsed = json.loads(raw.decode() or "{}")
    except (ValueError, UnicodeDecodeError):
        parsed = {}
    return status, parsed if isinstance(parsed, dict) else {}


def _github_headers(extra: Optional[dict] = None) -> dict:
    headers = {
        "Accept": "application/vnd.github+json",
        "X-GitHub-Api-Version": _GITHUB_API_VERSION,
        "User-Agent": "portunus",
    }
    headers.update(extra or {})
    return headers


class GitHubRotationAdapter:
    """GitHub -- REAL for OAuth App tokens the operator's own OAuth App
    issued; MANUAL / not-applicable for every other GitHub credential type
    (see the table above and docs/rotation.md).

    OAuth App token reset (`PATCH /applications/{client_id}/token`) is
    inherently destructive: GitHub replaces the token in place and the old
    one stops working the moment the call returns ("changes take effect
    immediately"). There is no add-new-then-retire path, so Portunus's
    non-destructive default cannot hold here -- the adapter refuses unless
    the caller passes `retire_old=True` (`--retire-old`) to acknowledge it.
    Because the old token is already dead once the reset returns, the new
    token is STORED FIRST and verified second (the reverse of the GCP
    adapter's verify-before-store, which only works when the old key is
    still valid).

    The ref must carry:
    - `tags["github_client_id"]` -- the OAuth App's client ID (not secret)
    - `tags["github_client_secret_ref"]` -- name of the registry reference
      holding that App's client secret. Resolved through
      `Resolver.resolve_call()` like every other value; never stored here.
    and must live in the local backend (`backend` "" or "local") -- the
    adapter refuses BEFORE resetting anything if it couldn't store the
    result, since a reset with nowhere to put the new token is a lockout.
    """

    def __init__(self, http=None, local_backend=None, audit=None, timeout: float = 30.0):
        self._http = http or _github_http
        self._local_backend = local_backend
        self._audit = audit
        self._timeout = timeout

    def capability(self) -> str:
        # Provider-level: a real path exists for (some) GitHub references.
        # Per-reference truth is capability_for(ref).
        return "auto"

    def capability_for(self, ref) -> str:
        """Per-reference capability from metadata only (never the value):
        "auto" / "manual" / "not_applicable", or "unknown" when the ref has
        no `github_token_type` tag to go on."""
        tags = getattr(ref, "tags", None) or {}
        subtype = github_token_subtype(tags=tags)
        cap = GITHUB_SUBTYPE_CAPABILITY.get(subtype, "unknown")
        if cap == "auto" and not tags.get("github_client_secret_ref"):
            # gho_ without the owning App's secret (e.g. a `gh auth login`
            # token) -- reset is impossible.
            return "manual"
        return cap

    def rotate(self, ref, resolver=None, retire_old: bool = False) -> RotationResult:
        if ref is None or not hasattr(ref, "sm_name"):
            raise RotationAdapterError(
                "GitHub rotation needs a registry reference (with tags), not a bare name"
            )
        if resolver is None:
            raise RotationAdapterError("GitHub rotation needs a resolver to read the stored token")
        ref_name = getattr(ref, "name", ref.sm_name)
        tags = getattr(ref, "tags", None) or {}

        template = "{{secret:" + ref_name + "}}"
        subtype = github_token_subtype(tags=tags) or resolver.resolve_call(
            template, lambda value: github_token_subtype(value=value)
        )
        if subtype != "oauth_app":
            reason = _GITHUB_SUBTYPE_REASON.get(subtype) or (
                "unrecognized GitHub credential -- set tags['github_token_type'] "
                "(classic_pat, fine_grained_pat, oauth_app, app_user, "
                "app_installation, app_private_key)"
            )
            raise RotationAdapterError(f"GitHub rotation not possible for {ref_name!r}: {reason}")

        client_id = tags.get("github_client_id", "")
        secret_ref = tags.get("github_client_secret_ref", "")
        if not client_id or not secret_ref:
            raise RotationAdapterError(
                f"GitHub rotation not possible for {ref_name!r}: an OAuth App token can only "
                "be reset by the App that issued it -- set tags['github_client_id'] and "
                "tags['github_client_secret_ref'] (a reference holding that App's client "
                "secret). A `gh auth login` token belongs to GitHub CLI's own OAuth App, "
                "so it is manual: re-run `gh auth login` and re-store it."
            )
        if not retire_old:
            raise RotationAdapterError(
                f"GitHub OAuth App token reset for {ref_name!r} invalidates the current token "
                "immediately (GitHub has no add-then-retire path) -- re-run with --retire-old "
                "to acknowledge."
            )
        if getattr(ref, "backend", "") not in ("", "local"):
            raise RotationAdapterError(
                f"GitHub rotation for {ref_name!r}: reference lives in backend "
                f"{ref.backend!r}, but this adapter can only store to the local vault -- "
                "refusing before reset, which would otherwise leave no valid token stored."
            )

        local_backend = self._local_backend
        if local_backend is None:
            from .localvault import LocalEncryptedBackend
            local_backend = LocalEncryptedBackend()

        secret_template = "{{secret:" + secret_ref + "}}"
        auth_id = resolver.resolve_call(
            secret_template,
            lambda client_secret: resolver.resolve_call(
                template,
                lambda old_token: self._reset_store_verify(
                    ref, ref_name, client_id, client_secret, old_token, local_backend
                ),
            ),
        )
        return RotationResult(
            provider="github",
            ref_name=ref_name,
            key_id=auth_id,
            phase="verified",
            retired_old=True,
        )

    def _app_call(self, method: str, client_id: str, client_secret: str, token: str):
        import base64

        basic = base64.b64encode(f"{client_id}:{client_secret}".encode()).decode()
        return self._http(
            method,
            f"{GITHUB_API}/applications/{client_id}/token",
            _github_headers({"Authorization": f"Basic {basic}", "Content-Type": "application/json"}),
            {"access_token": token},
            self._timeout,
        )

    def _audit_append(self, sm_name: str, result: str) -> None:
        if self._audit:
            self._audit.append("rotate:github-oauth-app", sm_name, result)

    def _reset_store_verify(self, ref, ref_name, client_id, client_secret, old_token, local_backend) -> str:
        old_token = old_token.strip()
        # Reset only works on a still-valid token -- check first so a dead
        # token gets a clear "re-authorize" answer instead of a bare 422.
        status, _ = self._app_call("POST", client_id, client_secret, old_token)
        if status == 404:
            self._audit_append(ref.sm_name, "err:old-token-invalid")
            raise RotationAdapterError(
                f"GitHub rotation for {ref_name!r}: the stored token is already invalid "
                "(GitHub's check-token returned 404) -- reset needs a valid token; the "
                "user must re-authorize the OAuth App."
            )
        if status != 200:
            self._audit_append(ref.sm_name, f"err:check-{status}")
            raise RotationAdapterError(
                f"GitHub rotation for {ref_name!r}: check-token returned HTTP {status} -- "
                "verify github_client_id / the client secret reference."
            )

        status, body = self._app_call("PATCH", client_id, client_secret, old_token)
        new_token = str(body.get("token") or "")
        if status != 200 or not new_token:
            self._audit_append(ref.sm_name, f"err:reset-{status}")
            raise RotationAdapterError(
                f"GitHub rotation for {ref_name!r}: reset returned HTTP {status}; nothing "
                "was changed on Portunus's side."
            )
        auth_id = str(body.get("id") or "")

        # The old token is dead from here on -- store before anything else.
        try:
            local_backend.store(ref.sm_name, new_token)
        except Exception as exc:
            self._audit_append(ref.sm_name, "err:store-failed-after-reset")
            raise RotationAdapterError(
                f"GitHub rotation for {ref_name!r}: GitHub reset the token but storing the "
                f"new one failed ({type(exc).__name__}) -- the old token is invalid; the "
                "user must re-authorize the OAuth App."
            ) from exc

        status, _ = self._app_call("POST", client_id, client_secret, new_token)
        if status != 200:
            self._audit_append(ref.sm_name, f"warn:verify-{status}")
            raise RotationAdapterError(
                f"GitHub rotation for {ref_name!r}: new token stored, but check-token "
                f"returned HTTP {status} for it -- investigate before relying on it."
            )
        self._audit_append(ref.sm_name, f"ok:auth={auth_id}" if auth_id else "ok")
        return auth_id


class StripeRotationAdapter:
    """Stripe -- STUB. No real calls."""

    def capability(self) -> str:
        return "unknown"

    def rotate(self, ref, resolver=None, retire_old: bool = False) -> None:
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
