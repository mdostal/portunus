"""ARCA — the vault store: Secret Manager backends behind one tiny interface.

ARCA (Roman strongbox/coffer) is the store itself — the GCP Secret Manager
tier here, plus the local-encrypted tier (DOS-448).

A backend answers exactly one dangerous question — "give me the plaintext for
this SM name" — and is called ONLY from the resolver, at the boundary. Keeping
it behind a tiny interface means tests use an in-memory ``MockBackend`` and
never touch GCP, while production uses ``GcloudBackend``.
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
