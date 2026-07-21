"""ARCA local-encrypted tier: values must round-trip, and plaintext must never
land on disk, in the key file, or survive decryption with the wrong key."""
import os
import stat

import pytest
from cryptography.fernet import Fernet

from portunus.backend import BackendError
from portunus.localvault import LocalEncryptedBackend

SECRET = "FAKE-TEST-VALUE-do-not-leak-0xBEEF"


def test_store_and_access_roundtrip(home):
    backend = LocalEncryptedBackend()
    backend.store("dostal-shared-anthropic", SECRET)
    assert backend.access("dostal-shared-anthropic") == SECRET


def test_vault_file_never_contains_plaintext(home):
    backend = LocalEncryptedBackend()
    backend.store("dostal-shared-anthropic", SECRET)
    raw = backend.vault_path.read_text()
    assert SECRET not in raw


def test_vault_and_key_files_are_0600(home):
    backend = LocalEncryptedBackend()
    backend.store("dostal-shared-anthropic", SECRET)
    assert stat.S_IMODE(os.stat(backend.vault_path).st_mode) == 0o600
    assert stat.S_IMODE(os.stat(backend.key_path).st_mode) == 0o600


def test_unknown_secret_raises_backend_error(home):
    backend = LocalEncryptedBackend()
    with pytest.raises(BackendError):
        backend.access("nope")


def test_key_persists_across_instances(home):
    b1 = LocalEncryptedBackend()
    b1.store("dostal-shared-anthropic", SECRET)
    b2 = LocalEncryptedBackend()
    assert b2.access("dostal-shared-anthropic") == SECRET


def test_wrong_master_key_fails_closed(home):
    b1 = LocalEncryptedBackend()
    b1.store("dostal-shared-anthropic", SECRET)
    # Simulate a swapped/corrupt master key: the vault must not decrypt.
    b1.key_path.write_bytes(Fernet.generate_key())
    b2 = LocalEncryptedBackend(vault_path=b1.vault_path, key_path=b1.key_path)
    with pytest.raises(BackendError):
        b2.access("dostal-shared-anthropic")


def test_remove_deletes_entry(home):
    backend = LocalEncryptedBackend()
    backend.store("dostal-shared-anthropic", SECRET)
    assert backend.remove("dostal-shared-anthropic") is True
    assert backend.remove("dostal-shared-anthropic") is False
    with pytest.raises(BackendError):
        backend.access("dostal-shared-anthropic")
