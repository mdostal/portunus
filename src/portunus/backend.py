"""ARCA — the vault store: Secret Manager backends behind one tiny interface.

ARCA (Roman strongbox/coffer) is the store itself — the GCP Secret Manager
tier here, the AWS Secrets Manager tier, an unimplemented Azure Key Vault
stub, plus the local-encrypted tier (DOS-448).

A backend answers exactly one dangerous question — "give me the plaintext for
this SM name" — and is called ONLY from the resolver, at the boundary. Keeping
it behind a tiny interface means tests use an in-memory ``MockBackend`` and
never touch a real cloud, while production shells to the provider's own CLI
(``gcloud`` / ``aws``), matching how ``bin/secrets`` always worked and adding
no new dependency.
"""
from __future__ import annotations

import shutil
import subprocess
from typing import Dict, Protocol, runtime_checkable


class BackendError(RuntimeError):
    """Raised when a backend cannot return a value (missing / access denied)."""


@runtime_checkable
class SecretBackend(Protocol):
    def access(self, sm_name: str) -> str:
        """Return the latest plaintext for `sm_name`, or raise BackendError."""
        ...


class MockBackend:
    """In-memory backend for tests and dry runs. Never touches the network."""

    def __init__(self, values: Dict[str, str] | None = None):
        self._values = dict(values or {})

    def set(self, sm_name: str, value: str) -> None:
        self._values[sm_name] = value

    def access(self, sm_name: str) -> str:
        try:
            return self._values[sm_name]
        except KeyError as exc:
            raise BackendError(f"unknown secret: {sm_name}") from exc


class GcloudBackend:
    """GCP Secret Manager via the gcloud CLI (matches bin/secrets exactly)."""

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


class AwsBackend:
    """AWS Secrets Manager via the aws CLI (same shell-out shape as GcloudBackend)."""

    def __init__(self, region: str = "", timeout: float = 30.0):
        self.region = region
        self.timeout = timeout

    def access(self, sm_name: str) -> str:
        if shutil.which("aws") is None:
            raise BackendError("aws CLI not found on PATH")
        cmd = [
            "aws", "secretsmanager", "get-secret-value",
            "--secret-id", sm_name,
            "--query", "SecretString",
            "--output", "text",
        ]
        if self.region:
            cmd.append(f"--region={self.region}")
        try:
            proc = subprocess.run(
                cmd, capture_output=True, text=True, timeout=self.timeout,
            )
        except subprocess.TimeoutExpired as exc:
            raise BackendError(f"aws timeout for {sm_name}") from exc
        if proc.returncode != 0:
            # stderr may name the secret but never contains the value.
            raise BackendError(
                f"aws access failed for {sm_name}: {proc.stderr.strip()[:200]}"
            )
        return proc.stdout.rstrip("\n")


class AzureBackend:
    """Azure Key Vault — unimplemented stub.

    Full Azure support is deferred to EPIC DOS-89. This class exists only so
    "azure" is a selectable, interface-conformant provider today: it compiles,
    satisfies SecretBackend structurally, and fails clearly instead of
    silently mis-behaving or making a real network call.
    """

    def __init__(self, vault_url: str = "", timeout: float = 30.0):
        self.vault_url = vault_url
        self.timeout = timeout

    def access(self, sm_name: str) -> str:
        raise BackendError(
            "AzureBackend is not implemented yet (tracked in EPIC DOS-89); "
            "select provider=gcp or provider=aws"
        )


_PROVIDERS = {
    "gcp": GcloudBackend,
    "aws": AwsBackend,
    "azure": AzureBackend,
}


def get_backend(provider: str, **kwargs) -> SecretBackend:
    """Factory: build the SecretBackend for `provider` ("gcp" | "aws" | "azure").

    kwargs are forwarded to the backend's constructor (e.g. project=, region=,
    vault_url=, timeout=).
    """
    try:
        cls = _PROVIDERS[provider]
    except KeyError as exc:
        raise BackendError(
            f"unknown provider: {provider!r} (want one of {sorted(_PROVIDERS)})"
        ) from exc
    return cls(**kwargs)
