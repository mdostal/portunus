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
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path
from typing import Dict, Optional, Protocol, runtime_checkable

from .auth import AuditChain, EnvOIDCTokenSource, GCPWorkloadIdentityAuth
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
    new vault-bindings.json -- never touches a legacy gcp-bindings.json."""
    bindings_path = _vault_bindings_path(path)
    bindings_path.parent.mkdir(parents=True, exist_ok=True)
    raw = {
        proj: {
            "wif_audience": b.wif_audience, "account": b.account,
            "backend": b.backend, "sync_mode": b.sync_mode,
        }
        for proj, b in bindings.items()
    }
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
            return value

        state = self._load_state()
        marker = latest_version(sm_name, project=project)
        if state.get(cache_key) == marker:
            try:
                return self.local.access(cache_key)
            except BackendError:
                pass  # marker recorded but local copy missing -- refetch below

        value = self.remote.access(sm_name, project=project)
        self.local.store(cache_key, value)
        state[cache_key] = marker
        self._save_state(state)
        return value
