"""Story 02/03 (portunus-chrome-credential-broker): Chrome's macOS Keychain key
derivation and the shared AES-CBC blob decryption both stories funnel through.

Secret-boundary invariant: nothing in this file prints, logs, or asserts-equal
against a raw decrypted value in a failure message -- every assertion either
checks structure/length or compares against an expected plaintext that is
itself test fixture data, never a real credential.
"""
import subprocess

import pytest
from cryptography.hazmat.primitives import hashes, padding
from cryptography.hazmat.primitives.ciphers import Cipher, algorithms, modes
from cryptography.hazmat.primitives.kdf.pbkdf2 import PBKDF2HMAC

from portunus.chrome_crypto import ChromeCryptoError, decrypt_blob, macos_key

MACOS_SALT = b"saltysalt"
MACOS_IV = b" " * 16


def _encrypt_like_chrome_macos(key: bytes, plaintext: str) -> bytes:
    padder = padding.PKCS7(algorithms.AES.block_size).padder()
    padded = padder.update(plaintext.encode("utf-8")) + padder.finalize()
    encryptor = Cipher(algorithms.AES(key), modes.CBC(MACOS_IV)).encryptor()
    return b"v10" + encryptor.update(padded) + encryptor.finalize()


def _derive_expected_key(password: bytes) -> bytes:
    kdf = PBKDF2HMAC(algorithm=hashes.SHA1(), length=16, salt=MACOS_SALT, iterations=1003)
    return kdf.derive(password)


def test_macos_key_derives_from_keychain_password_via_pbkdf2(monkeypatch):
    def fake_run(*args, **kwargs):
        return subprocess.CompletedProcess(args, 0, stdout="fixture-safe-storage-password\n", stderr="")

    monkeypatch.setattr(subprocess, "run", fake_run)

    key = macos_key()

    assert key == _derive_expected_key(b"fixture-safe-storage-password")
    assert len(key) == 16


def test_macos_key_raises_chrome_crypto_error_when_keychain_item_missing(monkeypatch):
    def fake_run(*args, **kwargs):
        raise subprocess.CalledProcessError(44, args[0] if args else ["security"])

    monkeypatch.setattr(subprocess, "run", fake_run)

    with pytest.raises(ChromeCryptoError):
        macos_key()


def test_decrypt_blob_round_trips_a_v10_blob():
    key = _derive_expected_key(b"fixture-safe-storage-password")
    blob = _encrypt_like_chrome_macos(key, "fixture-plaintext-value")

    assert decrypt_blob(key, blob) == "fixture-plaintext-value"


def test_decrypt_blob_rejects_unrecognized_prefix():
    key = _derive_expected_key(b"fixture-safe-storage-password")

    with pytest.raises(ChromeCryptoError):
        decrypt_blob(key, b"xx" + b"\x00" * 16)


def test_decrypt_blob_rejects_wrong_key_as_bad_padding_not_garbage():
    right_key = _derive_expected_key(b"fixture-safe-storage-password")
    wrong_key = _derive_expected_key(b"a-completely-different-password")
    blob = _encrypt_like_chrome_macos(right_key, "fixture-plaintext-value")

    with pytest.raises(ChromeCryptoError):
        decrypt_blob(wrong_key, blob)
