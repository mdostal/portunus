"""cli._build() honors PORTUNUS_BACKEND so provider selection (AC-1) is
reachable from the real entrypoint, not just importable from portunus.backend.
"""
import pytest

from portunus.backend import AwsBackend, AzureBackend, BackendError, GcloudBackend, MockBackend
from portunus.cli import _build
from portunus.localvault import LocalEncryptedBackend


def test_default_backend_is_local(home):
    *_, resolver = _build()
    assert isinstance(resolver.backend, LocalEncryptedBackend)


def test_portunus_backend_gcloud_alias(home, monkeypatch):
    monkeypatch.setenv("PORTUNUS_BACKEND", "gcloud")
    monkeypatch.setenv("PORTUNUS_GCP_PROJECT", "proj-1")
    *_, resolver = _build()
    assert isinstance(resolver.backend, GcloudBackend)
    assert resolver.backend.project == "proj-1"


def test_portunus_backend_gcp(home, monkeypatch):
    monkeypatch.setenv("PORTUNUS_BACKEND", "gcp")
    monkeypatch.setenv("PORTUNUS_GCP_PROJECT", "proj-1")
    *_, resolver = _build()
    assert isinstance(resolver.backend, GcloudBackend)
    assert resolver.backend.project == "proj-1"


def test_portunus_backend_aws(home, monkeypatch):
    monkeypatch.setenv("PORTUNUS_BACKEND", "aws")
    monkeypatch.setenv("PORTUNUS_AWS_REGION", "us-east-1")
    *_, resolver = _build()
    assert isinstance(resolver.backend, AwsBackend)
    assert resolver.backend.region == "us-east-1"


def test_portunus_backend_azure(home, monkeypatch):
    monkeypatch.setenv("PORTUNUS_BACKEND", "azure")
    monkeypatch.setenv("PORTUNUS_AZURE_VAULT_URL", "https://example.vault.azure.net")
    *_, resolver = _build()
    assert isinstance(resolver.backend, AzureBackend)
    assert resolver.backend.vault_url == "https://example.vault.azure.net"


def test_portunus_backend_mock_unchanged(home, monkeypatch):
    monkeypatch.setenv("PORTUNUS_BACKEND", "mock")
    monkeypatch.setenv("PORTUNUS_MOCK_DOSTAL_X", "OPAQUE-TEST-VALUE")
    *_, resolver = _build()
    assert isinstance(resolver.backend, MockBackend)
    assert resolver.backend.access("dostal-x") == "OPAQUE-TEST-VALUE"


def test_portunus_backend_unknown_raises(home, monkeypatch):
    monkeypatch.setenv("PORTUNUS_BACKEND", "bogus-cloud")
    with pytest.raises(BackendError):
        _build()
