"""ARCA — the vault store: pluggable Secret Manager backends behind one tiny interface.

ARCA (Roman strongbox/coffer) is the store itself — local-encrypted (default),
GCP Secret Manager (keyless via WIF), and AWS Secrets Manager (stub) tiers,
selected per-Reference by provider+project rather than one global choice.

A backend answers exactly one dangerous question — "give me the plaintext for
this SM name" — and is called ONLY from the resolver, at the boundary. Keeping
it behind a tiny interface means tests use an in-memory ``MockBackend`` and
never touch GCP/AWS, while production uses ``GcloudBackend``/(future)
``AWSSecretsManagerBackend``.
"""
from __future__ import annotations

import json
import os
import shutil
import subprocess
import tempfile
import time
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path
from typing import Dict, Optional, Protocol, runtime_checkable

from .auth import (
    AuditChain,
    AuthError,
    EnvOIDCTokenSource,
    GCPWorkloadIdentityAuth,
    OAuthAccessToken,
    OAuthRefreshTokenAuth,
)
from .filelock import flock_path
from .paths import home


class BackendError(RuntimeError):
    """Raised when a backend cannot return a value (missing / access denied)."""


@runtime_checkable
class SecretBackend(Protocol):
    def access(self, sm_name: str, project: str = "") -> str:
        """Return the latest plaintext for `sm_name`, or raise BackendError.

        `project` is an optional per-call override (a multi-project backend
        like GcloudBackend uses it to select the right project/credential
        binding; single-store backends ignore it).
        """
        ...


class MockBackend:
    """In-memory backend for tests and dry runs. Never touches the network."""

    def __init__(self, values: Dict[str, str] | None = None):
        self._values = dict(values or {})

    def set(self, sm_name: str, value: str) -> None:
        self._values[sm_name] = value

    def access(self, sm_name: str, project: str = "") -> str:
        try:
            return self._values[sm_name]
        except KeyError as exc:
            raise BackendError(f"unknown secret: {sm_name}") from exc


@dataclass(frozen=True)
class VaultBinding:
    """Which backend serves a project's secrets, and how to reach it.

    `backend` is one of "local" | "gcp" | "aws" -- which SecretBackend
    adapter this project routes to (see Resolver's per-reference router).
    `sync_mode` is "direct" (live-fetch every access, today's behavior) or
    "cached" (recency-aware pull-only sync-down into the local vault --
    never the reverse). Both default to today's exact effective behavior
    (gcp/direct) so every reference migrated from the legacy gcp-bindings.json
    format keeps working unchanged.

    `wif_audience` is the Workload Identity Federation provider resource name
    (e.g. "//iam.googleapis.com/projects/<num>/locations/global/
    workloadIdentityPools/<pool>/providers/<provider>") -- infrastructure
    topology, not a credential, but kept out of world-readable files anyway
    (see load_vault_bindings/save_vault_bindings 0600 handling). GCP-specific;
    ignored by other backend types.

    `account` is a local gcloud CLI identity email (e.g. "user@example.com")
    to pass as `--account=` when no WIF token is minted for this project --
    lets multiple already-locally-authenticated GCP accounts coexist without
    depending on gcloud's single mutable "active account" pointer. Empty
    means "use whatever gcloud considers active" (today's ambient behavior).
    GCP-specific; ignored by other backend types.
    """

    project: str
    wif_audience: str = ""
    account: str = ""
    backend: str = "gcp"
    sync_mode: str = "direct"


def _vault_bindings_path(path: Optional[Path] = None) -> Path:
    return path or (home() / "vault-bindings.json")


def _legacy_gcp_bindings_path() -> Path:
    return home() / "gcp-bindings.json"


def _vault_bindings_lock_path(path: Optional[Path] = None) -> Path:
    return _vault_bindings_path(path).with_suffix(".lock")


def load_vault_bindings(path: Optional[Path] = None) -> Dict[str, VaultBinding]:
    """Load PORTUNUS_HOME/vault-bindings.json (project -> VaultBinding).

    Migration-safe by construction, not by script: if the new file doesn't
    exist, falls back to reading the legacy PORTUNUS_HOME/gcp-bindings.json
    under its old schema, defaulting every entry to backend="gcp",
    sync_mode="direct" -- byte-for-byte today's real effective behavior. The
    legacy file is only ever read here, never written/moved/deleted by this
    function. Falls back further to a single binding derived from
    PORTUNUS_GCP_PROJECT/PORTUNUS_GCP_WIF_AUDIENCE when neither file exists --
    preserves today's zero-config single-project behavior exactly.
    """
    bindings_path = _vault_bindings_path(path)
    if bindings_path.exists():
        raw = json.loads(bindings_path.read_text() or "{}")
        return {
            proj: VaultBinding(
                project=proj,
                wif_audience=cfg.get("wif_audience", ""),
                account=cfg.get("account", ""),
                backend=cfg.get("backend", "gcp"),
                sync_mode=cfg.get("sync_mode", "direct"),
            )
            for proj, cfg in raw.items()
        }
    legacy_path = _legacy_gcp_bindings_path()
    if legacy_path.exists():
        raw = json.loads(legacy_path.read_text() or "{}")
        return {
            proj: VaultBinding(
                project=proj,
                wif_audience=cfg.get("wif_audience", ""),
                account=cfg.get("account", ""),
            )
            for proj, cfg in raw.items()
        }
    fallback_project = os.environ.get("PORTUNUS_GCP_PROJECT", "")
    if not fallback_project:
        return {}
    audience = os.environ.get("PORTUNUS_GCP_WIF_AUDIENCE", "")
    return {fallback_project: VaultBinding(project=fallback_project, wif_audience=audience)}


def save_vault_bindings(
    bindings: Dict[str, VaultBinding], path: Optional[Path] = None
) -> None:
    """Persist project bindings, 0600 on disk (grill H1). Always writes the
    new vault-bindings.json -- never touches a legacy gcp-bindings.json.
    Serialized via its own flock (portunus-vault-backup story 02) -- unlike
    registry.json/audit.log/vault.enc.json, this file previously had no
    lock file at all. This closes the write-time gap (two concurrent saves
    can no longer interleave their temp-file rename) and gives the
    coordinated snapshot primitive (backup.py) something to hold during a
    consistent read. NOTE: the full load-mutate-save cycle callers like
    `cmd_bindings_set` perform is not yet fully atomic across the *read* --
    that would need the same reload-inside-lock restructuring
    Registry._locked() already does, which is a bigger change than this
    story's scope (a dedicated lock file + the coordinated snapshot)."""
    bindings_path = _vault_bindings_path(path)
    bindings_path.parent.mkdir(parents=True, exist_ok=True)
    raw = {
        proj: {
            "wif_audience": b.wif_audience, "account": b.account,
            "backend": b.backend, "sync_mode": b.sync_mode,
        }
        for proj, b in bindings.items()
    }
    with flock_path(_vault_bindings_lock_path(path)):
        tmp = bindings_path.with_suffix(".json.tmp")
        tmp.write_text(json.dumps(raw, indent=2))
        os.chmod(tmp, 0o600)
        os.replace(tmp, bindings_path)
        os.chmod(bindings_path, 0o600)


class GcloudBackend:
    """GCP Secret Manager via the gcloud CLI.

    Zero-config: constructed with just `project` (or PORTUNUS_GCP_PROJECT),
    behaves exactly as before this epic -- ambient `gcloud` credentials,
    single project. Multi-project + keyless: pass `bindings` (from
    load_vault_bindings()); access(sm_name, project=...) then picks the
    matching binding's WIF audience and mints a short-lived access token per
    call, written to a 0600 tempfile passed via --access-token-file and
    unlinked in a finally block -- the token is never logged, printed, or
    returned.
    """

    def __init__(
        self,
        project: str = "",
        timeout: float = 30.0,
        credential_provider: Optional[GCPWorkloadIdentityAuth] = None,
        runner=None,
        bindings: Optional[Dict[str, VaultBinding]] = None,
        audit: Optional[AuditChain] = None,
    ):
        self.project = project
        self.timeout = timeout
        self.credential_provider = credential_provider
        self.runner = runner or subprocess.run
        self.bindings = bindings or {}
        self._audit = audit
        self._binding_providers: Dict[str, GCPWorkloadIdentityAuth] = {}
        for proj, binding in self.bindings.items():
            if binding.wif_audience:
                self._binding_providers[proj] = GCPWorkloadIdentityAuth(
                    audience=binding.wif_audience,
                    token_source=EnvOIDCTokenSource(),
                    audit=self._audit or AuditChain(),
                )

    def _credential_provider_for(self, project: str) -> Optional[GCPWorkloadIdentityAuth]:
        if project and project in self._binding_providers:
            return self._binding_providers[project]
        return self.credential_provider

    def access(self, sm_name: str, project: str = "") -> str:
        if shutil.which("gcloud") is None:
            raise BackendError("gcloud CLI not found on PATH")
        effective_project = project or self.project
        provider = self._credential_provider_for(effective_project)
        binding = self.bindings.get(effective_project)
        with self._access_token_file(provider) as token_file:
            cmd = ["gcloud"]
            if token_file:
                cmd.append(f"--access-token-file={token_file}")
            elif binding and binding.account:
                # Mutually exclusive with --access-token-file: a minted WIF
                # token already carries identity. --account selects which
                # already-locally-authenticated gcloud identity to use,
                # independent of gcloud's single mutable "active account".
                cmd.append(f"--account={binding.account}")
            cmd.extend(["secrets", "versions", "access", "latest", f"--secret={sm_name}"])
            if effective_project:
                cmd.append(f"--project={effective_project}")
            try:
                proc = self.runner(cmd, capture_output=True, text=True, timeout=self.timeout)
            except subprocess.TimeoutExpired as exc:
                raise BackendError(f"gcloud timeout for {sm_name}") from exc
        if proc.returncode != 0:
            # stderr may name the secret but never contains the value.
            raise BackendError(
                f"gcloud access failed for {sm_name}: {proc.stderr.strip()[:200]}"
            )
        return proc.stdout

    def latest_version(self, sm_name: str, project: str = "") -> str:
        """Return the latest VERSION's createTime -- a cheap, metadata-only
        recency marker for SyncingBackend, distinct from discover.py's
        create_time (the secret RESOURCE's creation time, which never
        changes on rotation). Never touches the value."""
        if shutil.which("gcloud") is None:
            raise BackendError("gcloud CLI not found on PATH")
        effective_project = project or self.project
        provider = self._credential_provider_for(effective_project)
        binding = self.bindings.get(effective_project)
        with self._access_token_file(provider) as token_file:
            cmd = ["gcloud"]
            if token_file:
                cmd.append(f"--access-token-file={token_file}")
            elif binding and binding.account:
                cmd.append(f"--account={binding.account}")
            cmd.extend(["secrets", "versions", "describe", "latest", f"--secret={sm_name}"])
            if effective_project:
                cmd.append(f"--project={effective_project}")
            cmd.append("--format=json")
            try:
                proc = self.runner(cmd, capture_output=True, text=True, timeout=self.timeout)
            except subprocess.TimeoutExpired as exc:
                raise BackendError(f"gcloud timeout for {sm_name}") from exc
        if proc.returncode != 0:
            raise BackendError(
                f"gcloud versions describe failed for {sm_name}: {proc.stderr.strip()[:200]}"
            )
        return json.loads(proc.stdout or "{}").get("createTime", "")

    @contextmanager
    def _access_token_file(self, provider: Optional[GCPWorkloadIdentityAuth]):
        if provider is None:
            yield None
            return
        minted = provider.mint()
        fd, path = tempfile.mkstemp(prefix="portunus-gcp-token-")
        try:
            os.fchmod(fd, 0o600)
            with os.fdopen(fd, "w") as fh:
                fh.write(minted.access_token)
            yield Path(path)
        finally:
            try:
                os.unlink(path)
            except OSError:
                pass


class AWSSecretsManagerBackend:
    """AWS Secrets Manager -- STUB. No real AWS calls.

    Interface-conformant placeholder so provider="aws" routes to a backend
    that fails clearly instead of silently mis-routing to GcloudBackend
    (grill V1: today's real gap is an unrecognized provider falling through
    to whatever _build() constructs by default and failing with a confusing
    GCP-flavored error against a non-GCP secret). AWSWebIdentityAuth
    (auth.py) is already ported and tested but intentionally NOT wired to
    this stub -- a future epic connects them.
    """

    def access(self, sm_name: str, project: str = "") -> str:
        raise BackendError(
            "AWS Secrets Manager backend is not yet implemented -- "
            "see portunus-vault-metadata design discussion"
        )


class VaultServerBackend:
    """HashiCorp Vault / OpenBao -- STUB. No real calls.

    Interface-conformant placeholder, matching AWSSecretsManagerBackend's
    own restraint. Researched (portunus-swappable-trio): Vault OSS is
    BUSL-1.1 -- that license applies to whoever *runs* the Vault server,
    not to this client-side adapter code, so it isn't a blocker for
    building a real adapter later; if BUSL terms matter to a given
    deployer, OpenBao (MPL-2.0, Linux Foundation fork, wire-compatible)
    is a self-hosted alternative a REST-based adapter would work against
    unchanged. This is informational, not a legal conclusion. No real
    Vault/OpenBao instance is validated against yet, so this stays a stub.
    """

    def access(self, sm_name: str, project: str = "") -> str:
        raise BackendError(
            "HashiCorp Vault backend is not yet implemented -- "
            "request it: https://github.com/mdostal/portunus/issues/new?template=adapter-request.yaml"
        )


class InfisicalBackend:
    """Infisical -- STUB. No real calls.

    Interface-conformant placeholder, matching AWSSecretsManagerBackend's
    own restraint. Researched (portunus-swappable-trio): Infisical's core
    (self-hosted secrets engine, CLI, SDKs) is genuinely MIT-licensed --
    the cleanest license of the researched candidates. Fully self-hostable
    and container-native (Docker Compose/Helm), fitting this project's
    no-OS-lock posture. No running Infisical instance is validated against
    yet, so this stays a stub.
    """

    def access(self, sm_name: str, project: str = "") -> str:
        raise BackendError(
            "Infisical backend is not yet implemented -- "
            "request it: https://github.com/mdostal/portunus/issues/new?template=adapter-request.yaml"
        )


class DopplerBackend:
    """Doppler -- STUB. No real calls.

    Interface-conformant placeholder, matching AWSSecretsManagerBackend's
    own restraint. Researched (portunus-swappable-trio): Doppler's CLI is
    Apache-2.0, but the actual secret store/service is closed-source,
    hosted SaaS with no viable self-host option for this project's
    Docker/podman-Linux, no-vendor-lock posture (its "on-prem" offering is
    sales-gated with no published architecture). Stays a stub until that
    changes or a concrete need appears.
    """

    def access(self, sm_name: str, project: str = "") -> str:
        raise BackendError(
            "Doppler backend is not yet implemented -- "
            "request it: https://github.com/mdostal/portunus/issues/new?template=adapter-request.yaml"
        )


class OnePasswordConnectBackend:
    """1Password Secrets Automation (Connect) -- STUB. No real calls.

    Interface-conformant placeholder, matching AWSSecretsManagerBackend's
    own restraint. Researched (portunus-swappable-trio): the Connect
    server ships as free, self-hostable container images, but is governed
    by 1Password's proprietary API Terms of Service (no OSS license) and
    requires a paid Business/Enterprise 1Password tenant just to mint the
    bootstrap credentials file. No provisioned tenant exists to validate
    against, so this stays a stub.
    """

    def access(self, sm_name: str, project: str = "") -> str:
        raise BackendError(
            "1Password Secrets Automation backend is not yet implemented -- "
            "request it: https://github.com/mdostal/portunus/issues/new?template=adapter-request.yaml"
        )


class AzureKeyVaultBackend:
    """Azure Key Vault -- STUB. No real calls.

    Interface-conformant placeholder, matching AWSSecretsManagerBackend's
    own restraint. Researched (portunus-swappable-trio): proprietary,
    cloud-only Azure PaaS -- no self-host story at all (consistent with
    that constraint, not a special case). A keyless adapter is possible
    via Entra ID Workload Identity Federation, but needs its own from-
    scratch Azure tenant/App Registration/federated-credential trust chain
    that doesn't reuse any of this project's existing GCP WIF plumbing.
    No Azure tenant is provisioned to validate against, so this stays a
    stub.
    """

    def access(self, sm_name: str, project: str = "") -> str:
        raise BackendError(
            "Azure Key Vault backend is not yet implemented -- "
            "request it: https://github.com/mdostal/portunus/issues/new?template=adapter-request.yaml"
        )


class OAuthBackend:
    """A generic OAuth 2.0 refresh->access token broker
    (portunus-oauth-token-broker). `sm_name` encodes `"<provider>:
    <account>"` -- an opaque, backend-specific identifier, the same role
    GCP Secret Manager's own sm_name already plays; no new SecretBackend
    Protocol parameter needed.

    `local_backend` (duck-typed -- e.g. `LocalEncryptedBackend` in
    practice, not imported directly here to avoid a circular import with
    localvault.py, mirroring SyncingBackend's own `local` parameter
    above) is where the credential bundle actually lives; this backend
    only ever calls its `load_oauth_credential(provider, account)`.
    Defaults to a real `LocalEncryptedBackend()` (deferred import) when
    not supplied, for standalone use; `_make_backend_router` passes the
    already-constructed shared "local" instance explicitly.

    Mints once per `sm_name`, then reuses the cached access token
    in-memory (this process only, never written to disk) until it's
    within `skew_seconds` of `expires_at` -- mirrors
    GCPWorkloadIdentityAuth/OIDCToken.expired()'s existing skew-check
    convention exactly.
    """

    def __init__(
        self,
        local_backend: Optional[object] = None,
        audit: Optional[AuditChain] = None,
        transport=None,
        skew_seconds: int = 30,
    ):
        if local_backend is None:
            from .localvault import LocalEncryptedBackend  # deferred: avoids a circular import
            local_backend = LocalEncryptedBackend()
        self.local_backend = local_backend
        self.audit = audit or AuditChain()
        self.transport = transport
        self.skew_seconds = skew_seconds
        self._cache: Dict[str, OAuthAccessToken] = {}

    def _cached(self, sm_name: str) -> Optional[OAuthAccessToken]:
        token = self._cache.get(sm_name)
        if token is None:
            return None
        if token.expires_at and token.expires_at <= int(time.time()) + self.skew_seconds:
            return None
        return token

    def access(self, sm_name: str, project: str = "") -> str:
        cached = self._cached(sm_name)
        if cached is not None:
            return cached.access_token

        if ":" not in sm_name:
            raise BackendError(
                f"oauth backend: sm_name must be 'provider:account', got {sm_name!r}"
            )
        provider, account = sm_name.split(":", 1)
        try:
            record = self.local_backend.load_oauth_credential(provider, account)
        except BackendError as exc:
            raise BackendError(f"oauth backend: {exc}") from exc

        credential = record["credential"]
        try:
            auth = OAuthRefreshTokenAuth(
                token_endpoint=credential["token_endpoint"],
                client_id=credential["client_id"],
                client_secret=credential["client_secret"],
                refresh_token=credential["refresh_token"],
                identity=sm_name,
                audit=self.audit,
                transport=self.transport,
            )
            minted = auth.mint()
        except (AuthError, KeyError) as exc:
            raise BackendError(
                f"oauth backend: could not mint access token for {sm_name}: {exc}"
            ) from exc

        self._cache[sm_name] = minted
        return minted.access_token


class SyncingBackend:
    """Recency-aware, pull-only sync-down cache -- GCP -> local only, never
    the reverse (portunus-vault-routing). Wraps `remote` (the real backend,
    e.g. GcloudBackend) + `local` (the cache tier, LocalEncryptedBackend in
    practice -- duck-typed here, not imported directly, to avoid a circular
    import with localvault.py) + a small on-disk SyncState file
    (`f"{project}:{sm_name}"` -> last-synced version marker, never a value).

    access(): if `remote` doesn't implement `latest_version`, always fetches
    live and still caches (correct, just not optimally cached -- keeps this
    wrapper generic for future adapters without a cheap recency check yet).
    Else, compares `remote.latest_version(...)` to the stored marker; a
    match serves straight from the local cache with zero remote value-fetch;
    a mismatch (or first sync) fetches the real value, caches it, and
    updates the marker.
    """

    def __init__(self, remote: SecretBackend, local: SecretBackend, state_path: Path):
        self.remote = remote
        self.local = local
        self.state_path = Path(state_path)
        # "synced" | "fresh" -- which path the most recent access() took.
        # Metadata about the cache's own behavior, never a value; lets
        # `portunus sync`/`portunus_sync` report synced vs. already_fresh
        # without re-deriving it from the state file.
        self.last_sync_result: str = ""

    def _load_state(self) -> Dict[str, str]:
        if self.state_path.exists():
            return json.loads(self.state_path.read_text() or "{}")
        return {}

    def _save_state(self, state: Dict[str, str]) -> None:
        self.state_path.parent.mkdir(parents=True, exist_ok=True)
        tmp = self.state_path.with_suffix(".json.tmp")
        tmp.write_text(json.dumps(state, indent=2))
        os.chmod(tmp, 0o600)
        os.replace(tmp, self.state_path)
        os.chmod(self.state_path, 0o600)

    def access(self, sm_name: str, project: str = "") -> str:
        cache_key = f"{project}:{sm_name}"
        latest_version = getattr(self.remote, "latest_version", None)
        if latest_version is None:
            value = self.remote.access(sm_name, project=project)
            self.local.store(cache_key, value)
            self.last_sync_result = "synced"
            return value

        state = self._load_state()
        try:
            marker = latest_version(sm_name, project=project)
        except BackendError as network_exc:
            # Remote unreachable (network/DNS/timeout). Serve the last-known-
            # good local copy if one exists, rather than hard-failing -- this
            # is what keeps a cached-mode project usable while disconnected.
            # Deliberately does NOT update the sync-state marker: the remote's
            # real current version was never confirmed, so writing it would
            # falsely mark the cache as verified-fresh once connectivity
            # returns. If there's no cached copy yet, there's genuinely
            # nothing to serve -- the ORIGINAL network error propagates, not
            # LocalEncryptedBackend's own "unknown secret" error.
            try:
                value = self.local.access(cache_key)
            except BackendError:
                raise network_exc from None
            self.last_sync_result = "stale-offline"
            return value
        if state.get(cache_key) == marker:
            try:
                value = self.local.access(cache_key)
                self.last_sync_result = "fresh"
                return value
            except BackendError:
                pass  # marker recorded but local copy missing -- refetch below

        value = self.remote.access(sm_name, project=project)
        self.local.store(cache_key, value)
        state[cache_key] = marker
        self.last_sync_result = "synced"
        self._save_state(state)
        return value
