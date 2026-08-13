"""ARCA local-encrypted tier: values must round-trip, and plaintext must never
land on disk, in the key file, or survive decryption with the wrong key."""
import json
import os
import stat
from datetime import datetime, timedelta, timezone

import pytest
from cryptography.fernet import Fernet

from portunus.backend import BackendError
from portunus.localvault import SESSION_SCHEMA, LocalEncryptedBackend, SessionExpired

SECRET = "FAKE-TEST-VALUE-do-not-leak-0xBEEF"
SESSION_SECRET = "SESSION-COOKIE-do-not-leak-0xDEAD"


def _store_expired_session(backend, site, account, ttl_seconds=60):
    """Directly write an already-expired session record -- avoids real
    sleeping in tests. Bypasses store_session()'s own (correct) expiry
    computation on purpose, to construct the past-tense case directly."""
    past = datetime.now(timezone.utc).replace(microsecond=0) - timedelta(seconds=10)
    past_iso = past.isoformat().replace("+00:00", "Z")
    record = {
        "schema": SESSION_SCHEMA,
        "namespace": {"site": site, "account": account},
        "ttl": {"seconds": ttl_seconds, "expires_at": past_iso},
        "rotation": {
            "generation": 1, "last_rotated_at": past_iso,
            "interval_seconds": None, "rotate_after": None,
        },
        "session": _session_state(),
    }
    backend.store(backend.session_key(site, account), json.dumps(record))


def _session_state():
    return {
        "cookies": [
            {
                "name": "sessionid",
                "value": SESSION_SECRET,
                "domain": "example.test",
                "path": "/",
                "httpOnly": True,
            }
        ],
        "origins": [
            {
                "origin": "https://example.test",
                "localStorage": [{"name": "token", "value": "bearer-token-do-not-leak"}],
            }
        ],
    }


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


def test_store_session_encrypts_state_under_site_account_namespace(home):
    backend = LocalEncryptedBackend()

    backend.store_session(
        "example.test",
        "dostal@example.test",
        _session_state(),
        ttl_seconds=3600,
        rotation_interval_seconds=900,
    )

    key = backend.session_key("example.test", "dostal@example.test")
    raw_vault = backend.vault_path.read_text()
    data = json.loads(raw_vault)
    assert key in data
    assert raw_vault.count(key) == 1
    assert SESSION_SECRET not in raw_vault
    assert "bearer-token-do-not-leak" not in raw_vault

    record = backend.load_session("example.test", "dostal@example.test")
    assert record["namespace"] == {
        "site": "example.test",
        "account": "dostal@example.test",
    }
    assert record["session"] == _session_state()


def test_inspect_session_returns_ttl_and_rotation_metadata_without_session(home):
    backend = LocalEncryptedBackend()
    backend.store_session(
        "example.test",
        "dostal@example.test",
        _session_state(),
        ttl_seconds=3600,
        rotation_interval_seconds=900,
    )

    inspection = backend.inspect_session("example.test", "dostal@example.test")

    assert inspection["schema"] == "portunus.session.v1"
    assert inspection["namespace"] == {
        "site": "example.test",
        "account": "dostal@example.test",
    }
    assert inspection["ttl"]["seconds"] == 3600
    assert inspection["ttl"]["expires_at"]
    assert inspection["rotation"]["interval_seconds"] == 900
    assert inspection["rotation"]["rotate_after"]
    assert inspection["rotation"]["generation"] == 1
    assert "session" not in inspection
    assert SESSION_SECRET not in json.dumps(inspection)


def test_store_session_does_not_log_or_print_plaintext(home, caplog, capsys):
    backend = LocalEncryptedBackend()

    backend.store_session(
        "example.test",
        "dostal@example.test",
        _session_state(),
        ttl_seconds=3600,
        rotation_interval_seconds=900,
    )

    captured = capsys.readouterr()
    assert SESSION_SECRET not in captured.out
    assert SESSION_SECRET not in captured.err
    assert SESSION_SECRET not in caplog.text
    assert "bearer-token-do-not-leak" not in captured.out
    assert "bearer-token-do-not-leak" not in captured.err
    assert "bearer-token-do-not-leak" not in caplog.text


# --- story 01: TTL enforcement ---------------------------------------------

def test_load_session_raises_session_expired_by_default(home):
    backend = LocalEncryptedBackend()
    _store_expired_session(backend, "example.test", "dostal@example.test")

    with pytest.raises(SessionExpired):
        backend.load_session("example.test", "dostal@example.test")


def test_session_expired_is_a_backend_error(home):
    """Existing `except BackendError` call sites must stay correct."""
    assert issubclass(SessionExpired, BackendError)


def test_load_session_allow_expired_bypasses_the_check(home):
    backend = LocalEncryptedBackend()
    _store_expired_session(backend, "example.test", "dostal@example.test")

    record = backend.load_session("example.test", "dostal@example.test", allow_expired=True)
    assert record["session"] == _session_state()


def test_inspect_session_never_raises_and_flags_expired_true(home):
    backend = LocalEncryptedBackend()
    _store_expired_session(backend, "example.test", "dostal@example.test")

    inspection = backend.inspect_session("example.test", "dostal@example.test")
    assert inspection["expired"] is True
    assert "session" not in inspection
    assert SESSION_SECRET not in json.dumps(inspection)


def test_inspect_session_flags_expired_false_for_a_valid_session(home):
    backend = LocalEncryptedBackend()
    backend.store_session(
        "example.test", "dostal@example.test", _session_state(), ttl_seconds=3600,
    )
    inspection = backend.inspect_session("example.test", "dostal@example.test")
    assert inspection["expired"] is False


def test_load_session_not_yet_expired_succeeds_normally(home):
    """Regression: a valid, non-expired session must load unchanged."""
    backend = LocalEncryptedBackend()
    backend.store_session(
        "example.test", "dostal@example.test", _session_state(), ttl_seconds=3600,
    )
    record = backend.load_session("example.test", "dostal@example.test")
    assert record["session"] == _session_state()


# --- story 02: list_sessions() ---------------------------------------------

def test_list_sessions_returns_metadata_for_multiple_sessions_with_correct_expired_flags(home):
    backend = LocalEncryptedBackend()
    backend.store_session("a.test", "user-a", _session_state(), ttl_seconds=3600)
    _store_expired_session(backend, "b.test", "user-b")

    sessions = backend.list_sessions()
    assert len(sessions) == 2

    by_site = {s["namespace"]["site"]: s for s in sessions}
    assert by_site["a.test"]["expired"] is False
    assert by_site["b.test"]["expired"] is True
    for s in sessions:
        assert "session" not in s
        assert SESSION_SECRET not in json.dumps(s)


def test_list_sessions_skips_corrupt_entries_but_returns_the_rest(home):
    backend = LocalEncryptedBackend()
    backend.store_session("a.test", "user-a", _session_state(), ttl_seconds=3600)
    # Store garbage directly under a session: key -- simulates a corrupt entry.
    backend.store(backend.session_key("corrupt.test", "user-c"), "not valid json{{{")

    sessions = backend.list_sessions()
    assert len(sessions) == 1
    assert sessions[0]["namespace"]["site"] == "a.test"


def test_list_sessions_excludes_non_session_vault_entries(home):
    backend = LocalEncryptedBackend()
    backend.store("dostal-shared-anthropic", SECRET)
    backend.store_session("a.test", "user-a", _session_state(), ttl_seconds=3600)

    sessions = backend.list_sessions()
    assert len(sessions) == 1
    assert sessions[0]["namespace"]["site"] == "a.test"


def test_inspect_session_output_unchanged_by_the_session_view_refactor(home):
    """Regression: inspect_session()'s existing fields must be identical
    after extracting _session_view() -- only `expired` is new."""
    backend = LocalEncryptedBackend()
    backend.store_session(
        "example.test", "dostal@example.test", _session_state(),
        ttl_seconds=3600, rotation_interval_seconds=900,
    )
    inspection = backend.inspect_session("example.test", "dostal@example.test")
    assert inspection["schema"] == "portunus.session.v1"
    assert inspection["namespace"] == {"site": "example.test", "account": "dostal@example.test"}
    assert inspection["ttl"]["seconds"] == 3600
    assert inspection["rotation"]["interval_seconds"] == 900
    assert set(inspection.keys()) == {"schema", "namespace", "ttl", "rotation", "expired"}
