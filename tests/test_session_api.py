import json
from datetime import datetime, timezone

import pytest

from portunus import LocalEncryptedBackend
from portunus.api import (
    SessionExpiredError,
    list_sessions,
    load_session,
    revoke_session,
    save_session,
)
from portunus.backend import BackendError

SESSION_SECRET = "SESSION-API-SECRET-do-not-leak"


def _session_payload():
    return {
        "cookies": [
            {
                "name": "sid",
                "value": SESSION_SECRET,
                "domain": "example.test",
                "path": "/",
            }
        ],
        "origins": [],
    }


def test_save_session_persists_via_arca_storage_model(home):
    backend = LocalEncryptedBackend()

    metadata = save_session(
        "example.test",
        "dostal@example.test",
        _session_payload(),
        ttl_seconds=3600,
        backend=backend,
    )

    assert metadata["id"] == "example.test/dostal@example.test"
    assert metadata["namespace"] == {
        "site": "example.test",
        "account": "dostal@example.test",
    }
    raw_vault = backend.vault_path.read_text()
    assert backend.session_key("example.test", "dostal@example.test") in json.loads(raw_vault)
    assert SESSION_SECRET not in raw_vault


def test_load_session_returns_active_session_data(home):
    backend = LocalEncryptedBackend()
    save_session(
        "example.test",
        "dostal@example.test",
        _session_payload(),
        ttl_seconds=3600,
        backend=backend,
    )

    assert load_session("example.test", "dostal@example.test", backend=backend) == _session_payload()


def test_load_session_refuses_expired_session(home, monkeypatch):
    backend = LocalEncryptedBackend()
    save_session(
        "example.test",
        "dostal@example.test",
        _session_payload(),
        ttl_seconds=1,
        backend=backend,
    )
    import portunus.api as api

    monkeypatch.setattr(
        api,
        "_utc_now",
        lambda: datetime(2100, 1, 1, tzinfo=timezone.utc),
    )

    with pytest.raises(SessionExpiredError):
        load_session("example.test", "dostal@example.test", backend=backend)


def test_list_sessions_returns_active_metadata_without_session_payload(home):
    backend = LocalEncryptedBackend()
    save_session(
        "example.test",
        "dostal@example.test",
        _session_payload(),
        ttl_seconds=3600,
        backend=backend,
    )
    save_session(
        "other.test",
        "dostal@example.test",
        {"token": "other-secret"},
        ttl_seconds=3600,
        backend=backend,
    )

    sessions = list_sessions(backend=backend)

    assert [session["id"] for session in sessions] == [
        "example.test/dostal@example.test",
        "other.test/dostal@example.test",
    ]
    assert all("session" not in session for session in sessions)
    serialized = json.dumps(sessions)
    assert SESSION_SECRET not in serialized
    assert "other-secret" not in serialized


def test_list_sessions_omits_expired_sessions_by_default(home, monkeypatch):
    backend = LocalEncryptedBackend()
    save_session(
        "example.test",
        "dostal@example.test",
        _session_payload(),
        ttl_seconds=1,
        backend=backend,
    )
    import portunus.api as api

    monkeypatch.setattr(
        api,
        "_utc_now",
        lambda: datetime(2100, 1, 1, tzinfo=timezone.utc),
    )

    assert list_sessions(backend=backend) == []
    assert list_sessions(backend=backend, include_expired=True)[0]["expired"] is True


def test_revoke_session_permanently_removes_session(home):
    backend = LocalEncryptedBackend()
    save_session(
        "example.test",
        "dostal@example.test",
        _session_payload(),
        ttl_seconds=3600,
        backend=backend,
    )

    assert revoke_session("example.test", "dostal@example.test", backend=backend) is True
    assert revoke_session("example.test", "dostal@example.test", backend=backend) is False
    with pytest.raises(BackendError):
        backend.load_session("example.test", "dostal@example.test")
