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
import sys
import tempfile
import time
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path
from typing import Dict, Optional, Protocol, runtime_checkable

from . import bitwarden, chrome_crypto, chrome_discovery, keystore
from .auth import (
    AuditChain,
    AuthError,
    EnvOIDCTokenSource,
    GCPWorkloadIdentityAuth,
    OAuthAccessToken,
    OAuthRefreshTokenAuth,
)
from .atomicio import atomic_write
from .filelock import flock_path
from .paths import home


class BackendError(RuntimeError):
    """Raised when a backend cannot return a value (missing / access denied)."""


# Markers a remote store's error carries when the SECRET itself is absent
# (as opposed to auth/network/CLI failures): GCP's gRPC status, AWS's
# exception name. Deliberately narrow -- "gcloud CLI not found on PATH" or
# "credential_file ... not found" must NOT count as a missing secret.
_NOT_FOUND_MARKERS = ("NOT_FOUND", "ResourceNotFoundException", "not found or has no versions")


def is_not_found(exc: BaseException) -> bool:
    """True when `exc` says the remote store has no such secret."""
    text = str(exc)
    return any(marker in text for marker in _NOT_FOUND_MARKERS)


def _local_has(local, sm_name: str) -> bool:
    """Key presence in the local tier, without decrypting when possible."""
    has = getattr(local, "has", None)
    if has is not None:
        return bool(has(sm_name))
    try:
        local.access(sm_name)
        return True
    except BackendError:
        return False


def _warn_local_only(audit, sm_name: str, where: str) -> None:
    """A reference was served from the local vault because its routed
    remote said NOT_FOUND. Loud on stderr (never stdout -- MCP stdio and
    `resolve --json` own stdout) and in the audit chain; never the value."""
    msg = (f"portunus: warning: {sm_name} is not in {where}; served the local-vault copy "
           f"instead (last_sync_result=local-only). Pin or migrate it: "
           f"`portunus doctor --fix-routing` (dry run), then add --apply")
    print(msg, file=sys.stderr)
    if audit is not None:
        try:
            audit.append("credential-access", sm_name, f"warn:local-only:not-in-{where}")
        except Exception:  # noqa: BLE001 - a warning must never fail the resolve
            pass


def _remote_hint(exc: BackendError, sm_name: str, where: str) -> BackendError:
    """Same error, plus where the value actually is and the one fix."""
    return BackendError(
        f"{exc} -- note: {sm_name} has a value in the LOCAL vault, but this reference "
        f"routes to {where}; run `portunus doctor --fix-routing` to pin or migrate it"
    )


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

    `impersonate_service_account` is a GCP service account email to pass as
    `--impersonate-service-account=` alongside `--account=`. Lets a stable,
    non-reauth-walled identity (e.g. a personal Google account with no org
    reauth policy) mint short-lived tokens for a project-scoped service
    account instead of depending on a human Workspace account's session,
    which can expire and demand an interactive reauth Portunus cannot
    perform itself. Requires the `account` identity to hold
    `roles/iam.serviceAccountTokenCreator` on the target service account.
    When `wif_audience` is also set, no gcloud identity is involved at all:
    the minted WIF federated token is exchanged for this service account's
    short-lived token via the IAM Credentials API (the pool principal needs
    `roles/iam.workloadIdentityUser` on it instead) -- the fully keyless,
    no-human-login path. Empty means "no impersonation." GCP-specific;
    ignored by other backend types.

    `credential_file` is a path to a gcloud-loadable credential file -- a
    service account key JSON or an external_account (WIF) config JSON --
    handed to gcloud through CLOUDSDK_AUTH_CREDENTIAL_FILE_OVERRIDE for this
    project only. Neither kind is subject to a human session's reauth
    policy, so this is the long-lived, non-interactive path for machines
    without a harness OIDC token. Only the path is stored here, never the
    file's contents; the file itself should be 0600. Used when no WIF token
    is minted; takes precedence over `account` (gcloud ignores `--account`
    under an override), and composes with `impersonate_service_account`.
    Empty means "not used." GCP-specific; ignored by other backend types.
    """

    project: str
    wif_audience: str = ""
    account: str = ""
    impersonate_service_account: str = ""
    backend: str = "gcp"
    sync_mode: str = "direct"
    credential_file: str = ""


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
                impersonate_service_account=cfg.get("impersonate_service_account", ""),
                backend=cfg.get("backend", "gcp"),
                sync_mode=cfg.get("sync_mode", "direct"),
                credential_file=cfg.get("credential_file", ""),
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
    raw = {
        proj: {
            "wif_audience": b.wif_audience, "account": b.account,
            "impersonate_service_account": b.impersonate_service_account,
            "backend": b.backend, "sync_mode": b.sync_mode,
            "credential_file": b.credential_file,
        }
        for proj, b in bindings.items()
    }
    with flock_path(_vault_bindings_lock_path(path)):
        atomic_write(bindings_path, json.dumps(raw, indent=2))


# Substrings gcloud prints when a user credential's session lapsed and only
# an interactive `gcloud auth login` can renew it. Matched case-insensitively
# against stderr (which names the secret at most, never a value).
_GCLOUD_REAUTH_MARKERS = (
    "reauthentication failed",
    "cannot prompt during non-interactive execution",
    "problem refreshing your current auth tokens",
    "reauthentication is needed",
    "please run:\n\n  $ gcloud auth login",
    "invalid_grant",
    "invalid_rapt",
)


def is_gcloud_reauth_error(stderr: str) -> bool:
    """True iff gcloud's stderr says the credential needs an interactive
    reauth -- the failure mode Portunus cannot fix by retrying."""
    text = (stderr or "").lower()
    return any(marker in text for marker in _GCLOUD_REAUTH_MARKERS)


def gcp_reauth_remediation(project: str, binding: Optional[VaultBinding] = None) -> str:
    """Self-service recovery text for a reauth-required failure. The first
    three options are non-interactive and stop the failure from recurring;
    the last one is the one-off interactive fallback. Names identities and
    paths only, never a credential."""
    proj = project or "<project>"
    account = (binding.account if binding else "") or "<account>"
    sa = (binding.impersonate_service_account if binding else "") or (
        f"<sa>@{proj}.iam.gserviceaccount.com"
    )
    return (
        f"GCP credentials for {proj} need an interactive reauth Portunus cannot perform. "
        "Non-interactive fixes (any one, then re-run `portunus health`): "
        f"(1) `portunus bindings set {proj} --credential-file <service-account-key-or-"
        "external-account.json>`; "
        f"(2) `portunus bindings set {proj} --account <identity-without-reauth-policy> "
        f"--impersonate-service-account {sa}`; "
        f"(3) `portunus bindings set {proj} --wif-audience <provider> "
        f"--impersonate-service-account {sa}`. "
        f"One-off interactive fallback: `gcloud auth login {account} --no-activate`."
    )


class GcpReauthRequired(BackendError):
    """gcloud refused because a user credential needs interactive reauth.
    The message carries gcp_reauth_remediation() so every surface (resolve,
    health, sync) shows the self-service fix, not just the raw gcloud text."""


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
                    # With WIF the impersonation hop happens in-process via
                    # the IAM Credentials API, not gcloud's flag.
                    service_account=binding.impersonate_service_account,
                )

    def _credential_provider_for(self, project: str) -> Optional[GCPWorkloadIdentityAuth]:
        if project and project in self._binding_providers:
            return self._binding_providers[project]
        return self.credential_provider

    @staticmethod
    def _identity(binding: Optional[VaultBinding], token_file) -> tuple:
        """(leading gcloud flags, extra env or None) for one call.

        Precedence: a minted WIF token file (already carries identity) >
        the binding's credential_file (SA key / external_account config via
        CLOUDSDK_AUTH_CREDENTIAL_FILE_OVERRIDE) > --account. Impersonation
        composes with the latter two. Nothing set = ambient gcloud."""
        if token_file:
            return [f"--access-token-file={token_file}"], None
        flags: list = []
        env = None
        if binding and binding.credential_file:
            path = Path(binding.credential_file).expanduser()
            if not path.is_file():
                raise BackendError(
                    f"credential_file for {binding.project} not found: {path}"
                )
            env = {**os.environ, "CLOUDSDK_AUTH_CREDENTIAL_FILE_OVERRIDE": str(path)}
        elif binding and binding.account:
            # --account selects which already-locally-authenticated gcloud
            # identity to use, independent of gcloud's single mutable
            # "active account".
            flags.append(f"--account={binding.account}")
        if binding and binding.impersonate_service_account and (
            binding.credential_file or binding.account
        ):
            # The base identity presents its own credential to mint a
            # short-lived token FOR this service account (requires
            # roles/iam.serviceAccountTokenCreator on the target) -- lets a
            # stable, non-reauth-walled identity stand in for a human
            # Workspace account whose session can expire.
            flags.append(f"--impersonate-service-account={binding.impersonate_service_account}")
        return flags, env

    def _run(self, cmd, env, **kwargs):
        # env only when set: injected test runners predate the kwarg.
        if env is not None:
            kwargs["env"] = env
        return self.runner(cmd, capture_output=True, text=True, timeout=self.timeout, **kwargs)

    def _failure(self, what: str, sm_name: str, project: str, stderr: str) -> BackendError:
        detail = f"gcloud {what} failed for {sm_name}: {(stderr or '').strip()[:200]}"
        if is_gcloud_reauth_error(stderr):
            return GcpReauthRequired(
                f"{detail} -- {gcp_reauth_remediation(project, self.bindings.get(project))}"
            )
        return BackendError(detail)

    def access(self, sm_name: str, project: str = "") -> str:
        if shutil.which("gcloud") is None:
            raise BackendError("gcloud CLI not found on PATH")
        effective_project = project or self.project
        provider = self._credential_provider_for(effective_project)
        binding = self.bindings.get(effective_project)
        with self._access_token_file(provider) as token_file:
            flags, env = self._identity(binding, token_file)
            cmd = ["gcloud", *flags]
            cmd.extend(["secrets", "versions", "access", "latest", f"--secret={sm_name}"])
            if effective_project:
                cmd.append(f"--project={effective_project}")
            try:
                proc = self._run(cmd, env)
            except subprocess.TimeoutExpired as exc:
                raise BackendError(f"gcloud timeout for {sm_name}") from exc
        if proc.returncode != 0:
            # stderr may name the secret but never contains the value.
            raise self._failure("access", sm_name, effective_project, proc.stderr)
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
            flags, env = self._identity(binding, token_file)
            cmd = ["gcloud", *flags]
            cmd.extend(["secrets", "versions", "describe", "latest", f"--secret={sm_name}"])
            if effective_project:
                cmd.append(f"--project={effective_project}")
            cmd.append("--format=json")
            try:
                proc = self._run(cmd, env)
            except subprocess.TimeoutExpired as exc:
                raise BackendError(f"gcloud timeout for {sm_name}") from exc
        if proc.returncode != 0:
            raise self._failure("versions describe", sm_name, effective_project, proc.stderr)
        return json.loads(proc.stdout or "{}").get("createTime", "")

    def add_version(self, sm_name: str, value: str, project: str = "") -> str:
        """Write `value` as a new version of an existing secret, under the
        same per-project identity selection as access(): WIF token file, or
        credential_file / --account [+ --impersonate-service-account], or
        ambient gcloud.

        The value reaches gcloud only on stdin (`--data-file=-`), never argv,
        so it can't show up in `ps` or a shell history. Returns gcloud's
        version resource name/number (not secret); errors carry gcloud's
        stderr, which never contains the value."""
        if shutil.which("gcloud") is None:
            raise BackendError("gcloud CLI not found on PATH")
        if not value:
            raise BackendError("refusing to add an empty secret version")
        effective_project = project or self.project
        provider = self._credential_provider_for(effective_project)
        binding = self.bindings.get(effective_project)
        with self._access_token_file(provider) as token_file:
            flags, env = self._identity(binding, token_file)
            cmd = ["gcloud", *flags]
            cmd.extend(["secrets", "versions", "add", sm_name, "--data-file=-"])
            if effective_project:
                cmd.append(f"--project={effective_project}")
            cmd.append("--format=value(name)")
            try:
                proc = self._run(cmd, env, input=value)
            except subprocess.TimeoutExpired as exc:
                raise BackendError(f"gcloud timeout adding a version to {sm_name}") from exc
        if proc.returncode != 0:
            raise self._failure("versions add", sm_name, effective_project, proc.stderr)
        return (proc.stdout or "").strip()

    def create_secret(self, sm_name: str, value: str, project: str = "") -> str:
        """Create `sm_name` (automatic replication) with `value` as its first
        version. Value on stdin only, same as add_version()."""
        if shutil.which("gcloud") is None:
            raise BackendError("gcloud CLI not found on PATH")
        if not value:
            raise BackendError("refusing to create a secret with an empty value")
        effective_project = project or self.project
        provider = self._credential_provider_for(effective_project)
        binding = self.bindings.get(effective_project)
        with self._access_token_file(provider) as token_file:
            flags, env = self._identity(binding, token_file)
            cmd = ["gcloud", *flags]
            cmd.extend(["secrets", "create", sm_name, "--data-file=-",
                        "--replication-policy=automatic"])
            if effective_project:
                cmd.append(f"--project={effective_project}")
            cmd.append("--format=value(name)")
            try:
                proc = self._run(cmd, env, input=value)
            except subprocess.TimeoutExpired as exc:
                raise BackendError(f"gcloud timeout creating {sm_name}") from exc
        if proc.returncode != 0:
            raise self._failure("secrets create", sm_name, effective_project, proc.stderr)
        return (proc.stdout or "").strip()

    def push(self, sm_name: str, value: str, project: str = "") -> str:
        """add_version(), creating the secret first if GCP says NOT_FOUND --
        the "move a local-only value into GCP" path (#246)."""
        try:
            return self.add_version(sm_name, value, project=project)
        except BackendError as exc:
            if not is_not_found(exc):
                raise
        return self.create_secret(sm_name, value, project=project)

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
                # Optional: a genuinely public/PKCE OAuth client (e.g. Codex
                # CLI's refresh grant) has no client_secret to supply -- see
                # OAuthRefreshTokenAuth's own doc comment. .get() rather than
                # [...] so a bundle that legitimately omits this key doesn't
                # KeyError before ever reaching the client.
                client_secret=credential.get("client_secret"),
                refresh_token=credential["refresh_token"],
                identity=sm_name,
                audit=self.audit,
                transport=self.transport,
                request_format=credential.get("request_format", "form"),
            )
            minted = auth.mint()
        except (AuthError, KeyError) as exc:
            raise BackendError(
                f"oauth backend: could not mint access token for {sm_name}: {exc}"
            ) from exc

        # Some providers (confirmed live for Codex CLI's refresh grant, see
        # OAuthAccessToken's own doc comment) rotate the refresh token on
        # every use -- the one just spent stops working for a future
        # refresh. Persist the new one immediately, or the very next
        # access() call (once this process's in-memory cache expires) mints
        # against an already-consumed token and fails. Best-effort: a
        # failure to persist the rotation must not fail THIS mint, which
        # already succeeded and has a perfectly good access token to
        # return -- log to the audit chain instead of raising.
        if minted.rotated_refresh_token:
            try:
                self.local_backend.store_oauth_credential(
                    provider, account, {**credential, "refresh_token": minted.rotated_refresh_token},
                )
                self.audit.append("credential-mint", sm_name, "ok:refresh-token-rotated")
            except Exception as exc:  # noqa: BLE001 - best-effort, see comment above
                self.audit.append("credential-mint", sm_name, f"warn:rotation-persist-failed:{exc}")

        self._cache[sm_name] = minted
        return minted.access_token


class ChromeBackend:
    """Reads Chrome's own saved logins through Portunus's existing resolver
    boundary (portunus-chrome-credential-broker). `sm_name` encodes
    `"chrome:<profile>:<origin>:<username>"` -- `<profile>` is Chrome's own
    profile directory name (e.g. "Default", "Profile 1"); `<origin>` and
    `<username>` identify one saved-login row.

    No access-token-style in-memory cache, unlike OAuthBackend: re-reading
    Login Data and re-decrypting is cheap (no network mint cost), and not
    caching minimizes how long a decrypted password lives in process
    memory.

    Platform dispatch (macOS Keychain vs. Linux Secret Service/KWallet) is
    chosen automatically from `sys.platform` -- callers never specify which.
    `user_data_dir` overrides Chrome's default profile root; only meant for
    tests, production callers leave it at the real default.
    """

    def __init__(self, audit: Optional[AuditChain] = None, user_data_dir: Optional[Path] = None):
        self.audit = audit or AuditChain()
        self.user_data_dir = user_data_dir

    def access(self, sm_name: str, project: str = "") -> str:
        # Can't naively str.split(":") -- an origin URL contains its own
        # colons ("https://", an optional port). Profile names never
        # contain ":", so split that off first; the username never contains
        # ":" either, so split the remainder at its LAST colon to recover
        # "<origin>:<username>" even when the origin itself has a port.
        malformed = BackendError(
            f"chrome backend: sm_name must be 'chrome:<profile>:<origin>:<username>', got {sm_name!r}"
        )
        if not sm_name.startswith("chrome:"):
            raise malformed
        profile_dir_name, _, remainder = sm_name[len("chrome:"):].partition(":")
        origin, _, username = remainder.rpartition(":")
        if not profile_dir_name or not origin or not username:
            raise malformed

        profiles = chrome_discovery.discover_profiles(self.user_data_dir)
        profile = next((p for p in profiles if p.dir_name == profile_dir_name), None)
        if profile is None:
            raise BackendError(f"chrome backend: no Chrome profile named {profile_dir_name!r} found")

        login_data_path = profile.user_data_dir / profile.dir_name / "Login Data"
        blob = chrome_crypto.read_password_blob(login_data_path, origin, username)
        if blob is None:
            raise BackendError(
                f"chrome backend: no saved login for {username!r} at {origin!r} in profile {profile_dir_name!r}"
            )

        if sys.platform == "darwin":
            key = chrome_crypto.macos_key()
        elif sys.platform.startswith("linux"):
            key = chrome_crypto.linux_key(profile.user_data_dir / profile.dir_name)
            if key is None:
                # Chrome's own no-keyring fallback: the password was never
                # really encrypted. Loud, not a silently-accepted decrypt --
                # see Story 03's design decision on why this is pushed to
                # the caller rather than swallowed inside chrome_crypto.
                self.audit.append(
                    "credential-access", sm_name,
                    "warn:no-keyring-found-chrome-stored-this-password-unprotected",
                )
        else:
            raise BackendError(
                f"chrome backend: {sys.platform!r} is not supported yet (macOS and Linux only) -- "
                "Windows's App-Bound Encryption has no sanctioned same-user read path today; "
                "see https://github.com/mdostal/portunus/issues/181"
            )

        try:
            return chrome_crypto.decrypt_blob(key, blob)
        except chrome_crypto.ChromeCryptoError as exc:
            raise BackendError(f"chrome backend: {exc}") from exc


class BitwardenBackend:
    """Reads one field of one item from the human's own Bitwarden vault via
    the official `bw` CLI (see bitwarden.py). `sm_name` encodes
    `"bitwarden:<item-id>[:<field>]"` -- field defaults to `password`;
    `username`, `totp` (current code), `notes`, or any custom field name also
    work. Read-only: Portunus never writes to the Bitwarden vault.

    Needs an unlocked vault (`BW_SESSION` exported by the human); fails
    closed with a remediation message otherwise. No in-memory cache, same
    rationale as ChromeBackend.
    """

    def __init__(self, audit: Optional[AuditChain] = None, runner=None):
        self.audit = audit or AuditChain()
        self.runner = runner

    def access(self, sm_name: str, project: str = "") -> str:
        try:
            item_id, fld = bitwarden.parse_sm_name(sm_name)
            return bitwarden.get_field(item_id, fld, runner=self.runner)
        except bitwarden.BitwardenError as exc:
            raise BackendError(str(exc)) from exc


class KeychainBackend:
    """Reads one item from the OS keystore -- macOS Keychain, Linux Secret
    Service (GNOME Keyring / KWallet) or Windows Credential Manager (see
    keystore.py). `sm_name` encodes `"keychain:<service>[:<account>]"`.
    Read-only, no in-memory cache (same rationale as ChromeBackend). A locked
    or unreachable keystore fails closed; nothing falls back to another store.
    """

    def __init__(self, audit: Optional[AuditChain] = None, runner=None,
                 platform: Optional[str] = None, win_reader=None):
        self.audit = audit or AuditChain()
        self.runner = runner
        self.platform = platform
        self.win_reader = win_reader

    def access(self, sm_name: str, project: str = "") -> str:
        try:
            service, account = keystore.parse_sm_name(sm_name)
            return keystore.read(service, account, platform=self.platform,
                                 runner=self.runner, win_reader=self.win_reader)
        except keystore.KeystoreError as exc:
            raise BackendError(str(exc)) from exc


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

    Local-only values (#246): `drop`/`prompt`/rotations write the local
    vault under the PLAIN `sm_name`; this cache tier uses
    `f"{project}:{sm_name}"`. When the remote says NOT_FOUND and there is
    no cache entry but a plain local value exists, that value is served
    with `last_sync_result="local-only"` and a stderr + audit warning --
    never a bare remote NOT_FOUND. Any other remote failure with a plain
    local value present raises with a hint naming the real location.
    """

    def __init__(self, remote: SecretBackend, local: SecretBackend, state_path: Path,
                 audit: Optional[AuditChain] = None, where: str = "gcp"):
        self.remote = remote
        self.local = local
        self.state_path = Path(state_path)
        self.audit = audit
        self.where = where
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
        atomic_write(self.state_path, json.dumps(state, indent=2))

    def _local_only(self, exc: BackendError, sm_name: str) -> str:
        """Remote failed and the cache tier has nothing: serve a plain local
        value on NOT_FOUND, else re-raise (with a hint if one exists)."""
        if not _local_has(self.local, sm_name):
            raise exc
        if not is_not_found(exc):
            raise _remote_hint(exc, sm_name, self.where) from exc
        value = self.local.access(sm_name)
        self.last_sync_result = "local-only"
        _warn_local_only(self.audit, sm_name, self.where)
        return value

    def access(self, sm_name: str, project: str = "") -> str:
        cache_key = f"{project}:{sm_name}"
        latest_version = getattr(self.remote, "latest_version", None)
        if latest_version is None:
            try:
                value = self.remote.access(sm_name, project=project)
            except BackendError as exc:
                return self._local_only(exc, sm_name)
            self.local.store(cache_key, value)
            self.last_sync_result = "synced"
            return value

        state = self._load_state()
        try:
            marker = latest_version(sm_name, project=project)
        except BackendError as remote_exc:
            if is_not_found(remote_exc):
                # The remote has no such secret: a cache entry is stale by
                # definition (deleted upstream), so only a plain local value
                # -- one that was never in the remote -- may be served.
                return self._local_only(remote_exc, sm_name)
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
                return self._local_only(remote_exc, sm_name)
            self.last_sync_result = "stale-offline"
            return value
        if state.get(cache_key) == marker:
            try:
                value = self.local.access(cache_key)
                self.last_sync_result = "fresh"
                return value
            except BackendError:
                pass  # marker recorded but local copy missing -- refetch below

        try:
            value = self.remote.access(sm_name, project=project)
        except BackendError as exc:
            return self._local_only(exc, sm_name)
        self.local.store(cache_key, value)
        state[cache_key] = marker
        self.last_sync_result = "synced"
        self._save_state(state)
        return value


class LocalFallbackBackend:
    """Wraps a remote store (GCP direct, AWS, ...) for references whose value
    may live in the local vault instead (#246): `access` asks the remote
    first; on a remote NOT_FOUND with a plain local value present, serves
    that with `last_sync_result="local-only"` and a stderr + audit warning.
    Any other remote failure re-raises, with a hint when a local value
    exists. The cached-gcp route gets the same behavior inside
    SyncingBackend itself."""

    def __init__(self, remote: SecretBackend, local: SecretBackend,
                 audit: Optional[AuditChain] = None, where: str = "remote"):
        self.remote = remote
        self.local = local
        self.audit = audit
        self.where = where
        self.last_sync_result: str = ""

    def __getattr__(self, name):
        # latest_version/add_version/... pass through to the real remote.
        if name == "remote":  # not yet set (copy/pickle): no recursion
            raise AttributeError(name)
        return getattr(self.remote, name)

    def access(self, sm_name: str, project: str = "") -> str:
        try:
            value = self.remote.access(sm_name, project=project)
        except BackendError as exc:
            if not _local_has(self.local, sm_name):
                raise
            if not is_not_found(exc):
                raise _remote_hint(exc, sm_name, self.where) from exc
            value = self.local.access(sm_name)
            self.last_sync_result = "local-only"
            _warn_local_only(self.audit, sm_name, self.where)
            return value
        self.last_sync_result = "remote"
        return value
