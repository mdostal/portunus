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
class GcpProjectBinding:
    """Which GCP project a Reference's secret lives in, and how to auth to it.

    `wif_audience` is the Workload Identity Federation provider resource name
    (e.g. "//iam.googleapis.com/projects/<num>/locations/global/
    workloadIdentityPools/<pool>/providers/<provider>") -- infrastructure
    topology, not a credential, but kept out of world-readable files anyway
    (see load_gcp_bindings/save_gcp_bindings 0600 handling).
    """

    project: str
    wif_audience: str = ""


def _gcp_bindings_path(path: Optional[Path] = None) -> Path:
    return path or (home() / "gcp-bindings.json")


def load_gcp_bindings(path: Optional[Path] = None) -> Dict[str, GcpProjectBinding]:
    """Load PORTUNUS_HOME/gcp-bindings.json (project -> GcpProjectBinding).

    Falls back to a single binding derived from PORTUNUS_GCP_PROJECT /
    PORTUNUS_GCP_WIF_AUDIENCE when no bindings file exists -- preserves
    today's zero-config single-project behavior exactly.
    """
    bindings_path = _gcp_bindings_path(path)
    if bindings_path.exists():
        raw = json.loads(bindings_path.read_text() or "{}")
        return {
            proj: GcpProjectBinding(project=proj, wif_audience=cfg.get("wif_audience", ""))
            for proj, cfg in raw.items()
        }
    fallback_project = os.environ.get("PORTUNUS_GCP_PROJECT", "")
    if not fallback_project:
        return {}
    audience = os.environ.get("PORTUNUS_GCP_WIF_AUDIENCE", "")
    return {fallback_project: GcpProjectBinding(project=fallback_project, wif_audience=audience)}


def save_gcp_bindings(
    bindings: Dict[str, GcpProjectBinding], path: Optional[Path] = None
) -> None:
    """Persist project bindings, 0600 on disk (grill H1)."""
    bindings_path = _gcp_bindings_path(path)
    bindings_path.parent.mkdir(parents=True, exist_ok=True)
    raw = {proj: {"wif_audience": b.wif_audience} for proj, b in bindings.items()}
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
    load_gcp_bindings()); access(sm_name, project=...) then picks the
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
        bindings: Optional[Dict[str, GcpProjectBinding]] = None,
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
        with self._access_token_file(provider) as token_file:
            cmd = ["gcloud"]
            if token_file:
                cmd.append(f"--access-token-file={token_file}")
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
