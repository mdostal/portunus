"""Five honest ARCA stubs (story 01, portunus-swappable-trio): Vault,
Infisical, Doppler, 1Password Connect, Azure Key Vault. Each mirrors
AWSSecretsManagerBackend's exact restraint -- access() unconditionally
raises BackendError, no real calls, no store()/latest_version() (nothing
calls those on an unrecognized/stub backend)."""
import pytest

from portunus.backend import (
    AzureKeyVaultBackend,
    BackendError,
    DopplerBackend,
    InfisicalBackend,
    OnePasswordConnectBackend,
    VaultServerBackend,
)

STUB_CLASSES = [
    (VaultServerBackend, "Vault"),
    (InfisicalBackend, "Infisical"),
    (DopplerBackend, "Doppler"),
    (OnePasswordConnectBackend, "1Password"),
    (AzureKeyVaultBackend, "Azure"),
]


@pytest.mark.parametrize("cls,name_fragment", STUB_CLASSES)
def test_stub_access_raises_never_calls_out(cls, name_fragment):
    backend = cls()
    with pytest.raises(BackendError) as exc_info:
        backend.access("sm-x", project="demo")
    msg = str(exc_info.value)
    assert name_fragment.lower() in msg.lower()
    assert "not yet implemented" in msg
    assert "request it" in msg.lower()


@pytest.mark.parametrize("cls,_name", STUB_CLASSES)
def test_stub_has_no_store_or_latest_version(cls, _name):
    """SecretBackend is a one-method Protocol -- these extras aren't part
    of it and nothing calls them on a stub (grill correction)."""
    backend = cls()
    assert not hasattr(backend, "store")
    assert not hasattr(backend, "latest_version")


@pytest.mark.parametrize("cls,_name", STUB_CLASSES)
def test_stub_docstring_notes_licensing_informationally(cls, _name):
    doc = cls.__doc__ or ""
    assert "STUB" in doc
    assert len(doc) > 100  # more than a one-liner -- sourced context, not just "not built"
