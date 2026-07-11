"""Local encrypted vault — encryption at rest, tamper detection, key handling."""
import json
import os
import stat

import pytest

from portunus.backend import BackendError
from portunus.localvault import (
    ALG,
    AutoKeyProvider,
    FileKeyProvider,
    KeychainKeyProvider,
    LocalKeyError,
    LocalVault,
    default_key_provider,
    open_sealed,
    seal,
)

SECRET = "sk-test-super-secret-value-12345"


@pytest.fixture
def vault(home, monkeypatch):
    monkeypatch.setenv("PORTUNUS_KEY_PROVIDER", "file")
    return LocalVault()


# --- seal/open primitives ---------------------------------------------------
def test_seal_open_roundtrip():
    master = b"\x01" * 32
    blob = seal(master, b"name:1", SECRET.encode())
    assert open_sealed(master, b"name:1", blob) == SECRET.encode()


def test_seal_output_contains_no_plaintext():
    blob = seal(b"\x01" * 32, b"name:1", SECRET.encode())
    assert SECRET not in json.dumps(blob)
    assert blob["alg"] == ALG


def test_open_rejects_wrong_key():
    blob = seal(b"\x01" * 32, b"name:1", SECRET.encode())
    with pytest.raises(Exception):
        open_sealed(b"\x02" * 32, b"name:1", blob)


def test_open_rejects_wrong_aad():
    """A blob cannot be replayed as a different secret/version."""
    master = b"\x01" * 32
    blob = seal(master, b"name:1", SECRET.encode())
    with pytest.raises(Exception):
        open_sealed(master, b"other:1", blob)


def test_open_rejects_tampered_ciphertext():
    master = b"\x01" * 32
    blob = seal(master, b"name:1", SECRET.encode())
    import base64
    ct = bytearray(base64.b64decode(blob["ct"]))
    ct[0] ^= 0xFF
    blob["ct"] = base64.b64encode(bytes(ct)).decode()
    with pytest.raises(Exception):
        open_sealed(master, b"name:1", blob)


# --- vault store -------------------------------------------------------------
def test_store_and_access_roundtrip(vault):
    vault.add_version("dostal-test-linear", SECRET)
    assert vault.access("dostal-test-linear") == SECRET


def test_value_is_encrypted_at_rest(vault, home):
    """THE load-bearing property: the plaintext never touches disk."""
    vault.add_version("dostal-test-linear", SECRET)
    found = []
    for root, _dirs, files in os.walk(home):
        for fname in files:
            raw = open(os.path.join(root, fname), "rb").read()
            if SECRET.encode() in raw:
                found.append(os.path.join(root, fname))
    assert not found, f"plaintext found on disk: {found}"


def test_vault_file_is_0600_and_dir_0700(vault):
    vault.add_version("dostal-test-linear", SECRET)
    path = vault.dir / "dostal-test-linear.json"
    assert stat.S_IMODE(path.stat().st_mode) == 0o600
    assert stat.S_IMODE(vault.dir.stat().st_mode) == 0o700


def test_versions_rotate_to_latest(vault):
    vault.add_version("dostal-test-linear", "old-value-1")
    n = vault.add_version("dostal-test-linear", SECRET)
    assert n == 2
    assert vault.access("dostal-test-linear") == SECRET
    assert vault.latest_version("dostal-test-linear") == 2


def test_tampered_vault_file_fails_closed(vault):
    vault.add_version("dostal-test-linear", SECRET)
    path = vault.dir / "dostal-test-linear.json"
    doc = json.loads(path.read_text())
    doc["versions"][-1]["tag"] = "0" * 64
    path.write_text(json.dumps(doc))
    with pytest.raises(BackendError, match="integrity"):
        vault.access("dostal-test-linear")


def test_missing_secret_raises(vault):
    with pytest.raises(BackendError, match="not found"):
        vault.access("dostal-test-nope")


def test_invalid_names_rejected(vault):
    for bad in ("../escape", "a/b", "", ".hidden;rm"):
        with pytest.raises(BackendError, match="invalid secret name"):
            vault.access(bad)


def test_delete_removes_ciphertext(vault):
    vault.add_version("dostal-test-linear", SECRET)
    assert vault.delete("dostal-test-linear") is True
    assert not (vault.dir / "dostal-test-linear.json").exists()
    assert vault.delete("dostal-test-linear") is False


def test_meta_stored_without_value(vault):
    vault.add_version("dostal-test-linear", SECRET,
                      meta={"description": "test", "project": "att", "environment": "dev"})
    meta = vault.meta("dostal-test-linear")
    assert meta == {"description": "test", "project": "att", "environment": "dev"}


# --- key providers -----------------------------------------------------------
def test_file_key_provider_creates_0600_and_is_stable(home):
    provider = FileKeyProvider()
    k1 = provider.key()
    assert len(k1) == 32
    assert stat.S_IMODE(provider.path.stat().st_mode) == 0o600
    assert FileKeyProvider().key() == k1


def test_wrong_master_key_cannot_decrypt(home, monkeypatch):
    monkeypatch.setenv("PORTUNUS_KEY_PROVIDER", "file")
    vault = LocalVault()
    vault.add_version("dostal-test-linear", SECRET)
    vault._provider.path.unlink()          # lose the key -> a NEW key is minted
    fresh = LocalVault()
    with pytest.raises(BackendError, match="integrity"):
        fresh.access("dostal-test-linear")


class _FakeKeychain:
    """A `security`-shaped runner backed by a dict. Never touches the real Keychain."""

    def __init__(self):
        self.items = {}

    def __call__(self, argv, input=None, capture_output=True, text=True):
        import subprocess
        if argv[:2] == ["security", "find-generic-password"]:
            account = argv[argv.index("-a") + 1]
            if account in self.items:
                return subprocess.CompletedProcess(argv, 0, stdout=self.items[account] + "\n", stderr="")
            return subprocess.CompletedProcess(argv, 44, stdout="", stderr="could not be found")
        if argv[:2] == ["security", "-i"]:
            parts = input.split()
            account = parts[parts.index("-a") + 1]
            self.items[account] = parts[parts.index("-w") + 1]
            return subprocess.CompletedProcess(argv, 0, stdout="", stderr="")
        raise AssertionError(f"unexpected security call: {argv}")


def test_keychain_provider_creates_and_reads_back(home):
    fake = _FakeKeychain()
    provider = KeychainKeyProvider(account="test-master", runner=fake)
    k1 = provider.key()
    assert len(k1) == 32
    assert KeychainKeyProvider(account="test-master", runner=fake).key() == k1
    # key hex went over stdin, is stored, and find returns it
    assert fake.items["test-master"] == k1.hex()


def test_auto_provider_prefers_keychain(home):
    fake = _FakeKeychain()
    provider = AutoKeyProvider(runner=fake)
    key = provider.key()
    assert fake.items  # stored in the (fake) keychain
    assert not provider._file.path.exists()
    assert AutoKeyProvider(runner=fake).key() == key


def test_auto_provider_falls_back_to_file_when_keychain_unusable(home, capsys):
    import subprocess

    def broken_security(argv, input=None, capture_output=True, text=True):
        return subprocess.CompletedProcess(argv, 1, stdout="", stderr="no default keychain")

    provider = AutoKeyProvider(runner=broken_security)
    key = provider.key()
    assert len(key) == 32
    assert provider._file.path.exists()
    assert "falling back to 0600 master-key file" in capsys.readouterr().err
    # stable on re-read, and does not retry the keychain once the file exists
    assert AutoKeyProvider(runner=broken_security).key() == key


def test_default_provider_env_override(home, monkeypatch):
    monkeypatch.setenv("PORTUNUS_KEY_PROVIDER", "file")
    assert isinstance(default_key_provider(), FileKeyProvider)
    monkeypatch.setenv("PORTUNUS_KEY_PROVIDER", "keychain")
    assert isinstance(default_key_provider(), KeychainKeyProvider)
    monkeypatch.setenv("PORTUNUS_KEY_PROVIDER", "bogus")
    with pytest.raises(LocalKeyError):
        default_key_provider()
