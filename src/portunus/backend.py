"""Secret Manager backends.

A backend answers exactly one dangerous question — "give me the plaintext for
this SM name" — and is called ONLY from the resolver, at the boundary. Keeping
it behind a tiny interface means tests use an in-memory ``MockBackend`` and
never touch GCP, while production uses ``GcloudBackend``.
"""
from __future__ import annotations

import shutil
import subprocess
import os
import tempfile
from contextlib import contextmanager
from pathlib import Path
from typing import Dict, Protocol, runtime_checkable

from .auth import GCPWorkloadIdentityAuth


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
    """GCP Secret Manager via the gcloud CLI.

    When a WIF auth provider is supplied, a short-lived access token is written
    to a temporary 0600 file and passed to gcloud with --access-token-file. The
    file is removed before access() returns.
    """

    def __init__(
        self,
        project: str = "",
        timeout: float = 30.0,
        credential_provider: GCPWorkloadIdentityAuth | None = None,
        runner=None,
    ):
        self.project = project
        self.timeout = timeout
        self.credential_provider = credential_provider
        self.runner = runner or subprocess.run

    def access(self, sm_name: str) -> str:
        if shutil.which("gcloud") is None:
            raise BackendError("gcloud CLI not found on PATH")
        with self._access_token_file() as token_file:
            cmd = ["gcloud"]
            if token_file:
                cmd.append(f"--access-token-file={token_file}")
            cmd.extend(["secrets", "versions", "access", "latest", f"--secret={sm_name}"])
            if self.project:
                cmd.append(f"--project={self.project}")
            try:
                proc = self.runner(
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

    @contextmanager
    def _access_token_file(self):
        if self.credential_provider is None:
            yield None
            return
        minted = self.credential_provider.mint()
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
