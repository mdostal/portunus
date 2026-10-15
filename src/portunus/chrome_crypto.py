"""Chrome saved-login decryption (portunus-chrome-credential-broker, Stories 02-03).

Platform-specific key fetch (``macos_key()``, ``linux_key()``) funneling into
one shared ``decrypt_blob()``. This module is the only place in the Chrome
backend that ever handles a decrypted password -- callers must be the
resolver's existing boundary sinks, never a CLI metadata command (those call
``chrome_discovery`` directly and never import this module at all).

macOS: Chrome's AES key is derived from a random password it stores in the
login Keychain under service "Chrome Safe Storage", account "Chrome"
(TALOS-2026-2444): PBKDF2-HMAC-SHA1, salt ``b"saltysalt"``, 1003 iterations,
128-bit key. Blobs are AES-128-CBC with Chromium's fixed static IV (16 ASCII
spaces) and a 3-byte ``v10``/``v11`` version prefix before the ciphertext.

Linux: the same blob format, but the key-fetch path differs by which keyring
backend Chrome picked at save time (Secret Service, KWallet, or -- when no
keyring is available -- Chrome's own fixed fallback password, which yields an
genuinely unencrypted-in-practice store; see ``linux_key()``).
"""
from __future__ import annotations

import subprocess

from cryptography.hazmat.primitives import hashes
from cryptography.hazmat.primitives.ciphers import Cipher, algorithms, modes
from cryptography.hazmat.primitives.kdf.pbkdf2 import PBKDF2HMAC

_SALT = b"saltysalt"
_IV = b" " * 16
_ITERATIONS = 1003
_KEY_LENGTH = 16


class ChromeCryptoError(Exception):
    """Raised for any Chrome key-fetch or blob-decryption failure. Never
    carries the raw secret material in its message -- only what step failed
    and why, so it's safe to let this propagate into a CLI error message."""


def _derive(password: bytes) -> bytes:
    kdf = PBKDF2HMAC(algorithm=hashes.SHA1(), length=_KEY_LENGTH, salt=_SALT, iterations=_ITERATIONS)
    return kdf.derive(password)


def macos_key() -> bytes:
    """Fetch Chrome's Safe Storage password from the macOS login Keychain
    (same-user, no-privilege-escalation read -- the same trust tier as
    Portunus's own local vault) and derive the AES key from it."""
    try:
        result = subprocess.run(
            ["security", "find-generic-password", "-w", "-s", "Chrome Safe Storage", "-a", "Chrome"],
            capture_output=True,
            check=True,
            text=True,
        )
    except (subprocess.CalledProcessError, FileNotFoundError) as exc:
        raise ChromeCryptoError(
            "could not read the 'Chrome Safe Storage' Keychain item -- is Chrome installed "
            "and has it saved at least one login on this Mac?"
        ) from exc
    return _derive(result.stdout.strip().encode("utf-8"))


def decrypt_blob(key: bytes, blob: bytes) -> str:
    """Decrypt one Login Data password_value blob given its AES key."""
    prefix, ciphertext = blob[:3], blob[3:]
    if prefix not in (b"v10", b"v11"):
        raise ChromeCryptoError(f"unrecognized Chrome password blob prefix: {prefix!r}")

    try:
        decryptor = Cipher(algorithms.AES(key), modes.CBC(_IV)).decryptor()
        padded = decryptor.update(ciphertext) + decryptor.finalize()
    except ValueError as exc:
        raise ChromeCryptoError("failed to AES-decrypt the saved-login blob -- wrong key or corrupted data") from exc

    pad_len = padded[-1] if padded else 0
    if not padded or not (1 <= pad_len <= 16) or padded[-pad_len:] != bytes([pad_len]) * pad_len:
        raise ChromeCryptoError("decrypted blob has invalid PKCS7 padding -- wrong key or corrupted data")

    try:
        return padded[:-pad_len].decode("utf-8")
    except UnicodeDecodeError as exc:
        raise ChromeCryptoError("decrypted blob is not valid UTF-8 -- wrong key or corrupted data") from exc
