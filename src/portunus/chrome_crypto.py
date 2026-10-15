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

import shutil
import sqlite3
import subprocess
import tempfile
from pathlib import Path
from typing import Optional

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


def _linux_application_name(profile_dir: Path) -> str:
    return "chromium" if "chromium" in str(profile_dir).lower() else "chrome"


def _run_or_none(cmd: list) -> Optional[str]:
    try:
        result = subprocess.run(cmd, capture_output=True, text=True)
    except FileNotFoundError:
        return None
    if result.returncode != 0 or not result.stdout.strip():
        return None
    return result.stdout.rstrip("\n")


def linux_key(profile_dir: Path) -> Optional[bytes]:
    """Fetch Chrome's Safe Storage password on Linux, trying Secret Service
    (libsecret) first, then KWallet, mirroring Chromium's own auto-selection
    by desktop environment (docs/linux/password_storage.md).

    Returns None when neither keyring is available -- a real, common state
    on minimal Linux setups, never an error, never a fabricated key. The
    caller (ChromeBackend/CLI) is responsible for surfacing that loudly;
    this function only reports the fact.
    """
    app = _linux_application_name(profile_dir)

    password = _run_or_none(["secret-tool", "lookup", "application", app])
    if password is None:
        password = _run_or_none(
            ["kwallet-query", "-f", "Chrome Keys", "-r", "Chrome Safe Storage", "kdewallet"]
        )
    if password is None:
        return None
    return _derive(password.encode("utf-8"))


def decrypt_blob(key: Optional[bytes], blob: bytes) -> str:
    """Decrypt one Login Data password_value blob given its AES key.

    key=None is the no-keyring fallback state linux_key() can return: Chrome
    stored the value unprotected, marked with a v10 prefix the same as a real
    encrypted blob. A v11 blob always means Chrome actually encrypted it, so
    key=None against a v11 blob is a real error, not a quieter decrypt.
    """
    prefix, payload = blob[:3], blob[3:]
    if prefix not in (b"v10", b"v11"):
        raise ChromeCryptoError(f"unrecognized Chrome password blob prefix: {prefix!r}")

    if key is None:
        if prefix != b"v10":
            raise ChromeCryptoError(
                "no key available (no Secret Service/KWallet found), but this blob is v11 -- "
                "Chrome actually encrypted it, so it cannot be read as the no-keyring plaintext fallback"
            )
        try:
            return payload.decode("utf-8")
        except UnicodeDecodeError as exc:
            raise ChromeCryptoError("no-keyring fallback blob is not valid UTF-8 -- corrupted data") from exc

    ciphertext = payload
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


def read_password_blob(login_data_path: Path, origin_url: str, username_value: str) -> Optional[bytes]:
    """Copy Login Data (same discipline as chrome_discovery.list_logins --
    never open the live file) and return the raw password_value blob for one
    (origin_url, username_value) row, or None if no such row exists.

    This is the only function in the whole Chrome backend allowed to select
    password_value -- chrome_discovery.py never does, by design, so the
    metadata-only CLI commands that only ever call chrome_discovery stay
    structurally incapable of touching this.
    """
    if not login_data_path.is_file():
        return None

    with tempfile.TemporaryDirectory(prefix="portunus-chrome-") as tmp_dir:
        tmp_copy = Path(tmp_dir) / "Login Data"
        shutil.copy2(login_data_path, tmp_copy)
        conn = sqlite3.connect(str(tmp_copy))
        try:
            row = conn.execute(
                "SELECT password_value FROM logins WHERE origin_url = ? AND username_value = ? LIMIT 1",
                (origin_url, username_value),
            ).fetchone()
        finally:
            conn.close()

    return row[0] if row is not None else None
