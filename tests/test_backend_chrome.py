"""Story 04 (portunus-chrome-credential-broker): ChromeBackend, its dispatch
through _make_backend_router (by ref.backend, the same mechanism OAuthBackend
is already selected by -- not by parsing sm_name text, which is how this
story's own planning doc originally (and incorrectly) described cli.py's
dispatch; corrected here against the real code), and the two metadata CLI
commands' structural zero-password-material guarantee.
"""
import json
import sqlite3
import sys
from types import SimpleNamespace

import pytest

from portunus import chrome_crypto
from portunus.audit import AuditChain
from portunus.backend import BackendError, ChromeBackend


def _write_local_state(user_data_dir, profiles):
    local_state = {"profile": {"info_cache": {name: {"name": display} for name, display in profiles.items()}}}
    (user_data_dir / "Local State").write_text(json.dumps(local_state))


def _write_login_data(path, rows):
    conn = sqlite3.connect(str(path))
    conn.execute(
        "CREATE TABLE logins (origin_url TEXT, username_value TEXT, password_value BLOB, "
        "signon_realm TEXT, date_created INTEGER)"
    )
    for origin, username, pw in rows:
        conn.execute(
            "INSERT INTO logins (origin_url, username_value, password_value, signon_realm, date_created) "
            "VALUES (?, ?, ?, ?, ?)",
            (origin, username, pw, origin, 0),
        )
    conn.commit()
    conn.close()


@pytest.fixture
def user_data_dir(tmp_path):
    d = tmp_path / "User Data"
    d.mkdir()
    _write_local_state(d, {"Default": "Person 1"})
    (d / "Default").mkdir()
    return d


def test_chrome_backend_access_happy_path_macos(user_data_dir, monkeypatch):
    key = b"0123456789abcdef"  # 16-byte fixture AES key

    from cryptography.hazmat.primitives import padding
    from cryptography.hazmat.primitives.ciphers import Cipher, algorithms, modes

    padder = padding.PKCS7(algorithms.AES.block_size).padder()
    padded = padder.update(b"fixture-password-123") + padder.finalize()
    encryptor = Cipher(algorithms.AES(key), modes.CBC(b" " * 16)).encryptor()
    blob = b"v10" + encryptor.update(padded) + encryptor.finalize()

    _write_login_data(
        user_data_dir / "Default" / "Login Data",
        [("https://example.com", "alice@example.com", blob)],
    )

    monkeypatch.setattr(sys, "platform", "darwin")
    monkeypatch.setattr(chrome_crypto, "macos_key", lambda: key)

    backend = ChromeBackend(audit=AuditChain(), user_data_dir=user_data_dir)
    result = backend.access("chrome:Default:https://example.com:alice@example.com")

    assert result == "fixture-password-123"


def test_chrome_backend_raises_specific_error_when_no_matching_login(user_data_dir, monkeypatch):
    _write_login_data(user_data_dir / "Default" / "Login Data", [])
    monkeypatch.setattr(sys, "platform", "darwin")

    backend = ChromeBackend(audit=AuditChain(), user_data_dir=user_data_dir)

    with pytest.raises(BackendError, match="no saved login"):
        backend.access("chrome:Default:https://nope.example.com:nobody@example.com")


def test_chrome_backend_rejects_malformed_sm_name(user_data_dir):
    backend = ChromeBackend(audit=AuditChain(), user_data_dir=user_data_dir)

    with pytest.raises(BackendError):
        backend.access("chrome:not-enough-parts")


def test_chrome_backend_warns_in_audit_when_linux_key_is_none(user_data_dir, monkeypatch):
    """Story 03's deferred acceptance criterion: the no-keyring fallback must
    be loud. ChromeBackend is the real caller that must surface it."""
    blob = b"v10" + b"fixture-plaintext-123"
    _write_login_data(
        user_data_dir / "Default" / "Login Data",
        [("https://example.com", "alice@example.com", blob)],
    )

    monkeypatch.setattr(sys, "platform", "linux")
    monkeypatch.setattr(chrome_crypto, "linux_key", lambda profile_dir: None)

    audit = AuditChain()
    backend = ChromeBackend(audit=audit, user_data_dir=user_data_dir)
    result = backend.access("chrome:Default:https://example.com:alice@example.com")

    assert result == "fixture-plaintext-123"
    entries = [e for e in audit.entries() if "no-keyring" in e.get("result", "")]
    assert entries, "expected an audit entry warning about the no-keyring fallback"


def test_make_backend_router_dispatches_chrome_by_ref_backend_field():
    from portunus.cli import _make_backend_router

    fallback = SimpleNamespace()
    router = _make_backend_router({}, AuditChain(), fallback_backend=fallback)
    ref = SimpleNamespace(backend="chrome", project="")

    resolved = router(ref)

    assert isinstance(resolved, ChromeBackend)
