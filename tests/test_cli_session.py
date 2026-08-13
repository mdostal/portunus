"""portunus session store|load|inspect|list|remove (story 01,
portunus-session-cli). session store mirrors drop's stdin-only-in
discipline; session load mirrors resolve's tempfile-only-out discipline --
a session record is exactly as sensitive as a secret value."""
import json
import os
import stat
from datetime import datetime, timedelta, timezone

import pytest

from portunus.audit import AuditChain
from portunus.cli import main
from portunus.localvault import SESSION_SCHEMA, LocalEncryptedBackend

SESSION_SECRET = "SESSION-COOKIE-do-not-leak-0xDEAD"


def _session_json():
    return json.dumps({
        "cookies": [{"name": "sessionid", "value": SESSION_SECRET, "domain": "example.test"}],
    })


def _store_expired_directly(home, site="example.test", account="dostal@example.test"):
    """Bypass the CLI to write an already-expired record -- avoids real
    sleeping in tests, same technique as test_localvault.py."""
    backend = LocalEncryptedBackend()
    past = datetime.now(timezone.utc).replace(microsecond=0) - timedelta(seconds=10)
    past_iso = past.isoformat().replace("+00:00", "Z")
    record = {
        "schema": SESSION_SCHEMA,
        "namespace": {"site": site, "account": account},
        "ttl": {"seconds": 60, "expires_at": past_iso},
        "rotation": {"generation": 1, "last_rotated_at": past_iso,
                     "interval_seconds": None, "rotate_after": None},
        "session": json.loads(_session_json()),
    }
    backend.store(backend.session_key(site, account), json.dumps(record))


@pytest.fixture(autouse=True)
def _local_backend(monkeypatch):
    monkeypatch.delenv("PORTUNUS_BACKEND", raising=False)


def _store(home, ttl_seconds=3600, site="example.test", account="dostal@example.test"):
    value_file = home / "session.json"
    value_file.write_text(_session_json())
    rc = main(["session", "store", site, account, "--value-file", str(value_file),
               "--ttl-seconds", str(ttl_seconds)])
    return rc


def test_session_store_confirms_by_namespace_only(home, capsys):
    rc = _store(home)
    captured = capsys.readouterr()
    assert rc == 0
    assert "example.test" in captured.out
    assert SESSION_SECRET not in captured.out
    assert SESSION_SECRET not in captured.err


def test_session_store_malformed_json_fails_closed(home, capsys):
    value_file = home / "bad.json"
    value_file.write_text("not valid json{{{")
    rc = main(["session", "store", "example.test", "dostal@example.test",
               "--value-file", str(value_file), "--ttl-seconds", "3600"])
    err = capsys.readouterr().err
    assert rc != 0
    assert "json" in err.lower() or "invalid" in err.lower()


def test_session_load_writes_tempfile_and_prints_path_only(home, capsys):
    _store(home)
    capsys.readouterr()
    rc = main(["session", "load", "example.test", "dostal@example.test"])
    captured = capsys.readouterr()
    assert rc == 0
    path = captured.out.strip()
    assert SESSION_SECRET not in captured.out
    assert SESSION_SECRET not in captured.err

    mode = stat.S_IMODE(os.stat(path).st_mode)
    assert mode == 0o600
    record = json.loads(open(path).read())
    assert record["session"]["cookies"][0]["value"] == SESSION_SECRET
    os.unlink(path)


def test_session_load_expired_refuses_without_allow_expired(home, capsys):
    _store_expired_directly(home)

    rc = main(["session", "load", "example.test", "dostal@example.test"])
    err = capsys.readouterr().err
    assert rc != 0
    assert "--allow-expired" in err
    assert SESSION_SECRET not in err


def test_session_load_expired_with_allow_expired_succeeds(home, capsys):
    _store_expired_directly(home)

    rc = main(["session", "load", "example.test", "dostal@example.test", "--allow-expired"])
    captured = capsys.readouterr()
    assert rc == 0
    path = captured.out.strip()
    os.unlink(path)


def test_session_inspect_prints_metadata_only(home, capsys):
    _store(home)
    capsys.readouterr()
    rc = main(["session", "inspect", "example.test", "dostal@example.test", "--json"])
    out = capsys.readouterr().out
    assert rc == 0
    data = json.loads(out)
    assert data["namespace"]["site"] == "example.test"
    assert "session" not in data
    assert SESSION_SECRET not in out


def test_session_list_shows_all_sessions_never_a_payload(home, capsys):
    _store(home, site="a.test", account="user-a")
    _store(home, site="b.test", account="user-b")
    capsys.readouterr()
    rc = main(["session", "list", "--json"])
    out = capsys.readouterr().out
    assert rc == 0
    data = json.loads(out)
    assert len(data) == 2
    assert SESSION_SECRET not in out


def test_session_remove_confirms_by_namespace_and_actually_removes(home, capsys):
    _store(home)
    rc = main(["session", "remove", "example.test", "dostal@example.test"])
    captured = capsys.readouterr()
    assert rc == 0
    assert "example.test" in captured.out

    rc2 = main(["session", "inspect", "example.test", "dostal@example.test"])
    assert rc2 != 0


def test_session_store_load_remove_write_audit_entries_inspect_list_do_not(home, capsys):
    _store(home)
    capsys.readouterr()
    main(["session", "load", "example.test", "dostal@example.test"])
    out = capsys.readouterr().out
    os.unlink(out.strip())
    main(["session", "inspect", "example.test", "dostal@example.test"])
    capsys.readouterr()
    main(["session", "list"])
    capsys.readouterr()
    main(["session", "remove", "example.test", "dostal@example.test"])
    capsys.readouterr()

    audit = AuditChain()
    actions = [e["action"] for e in audit.entries()]
    assert actions.count("session_store") == 1
    assert actions.count("session_load") == 1
    assert actions.count("session_remove") == 1
    assert "session_inspect" not in actions
    assert "session_list" not in actions
    for e in audit.entries():
        assert SESSION_SECRET not in json.dumps(e)
    assert audit.verify() is True


def test_session_commands_require_local_backend(home, capsys, monkeypatch):
    monkeypatch.setenv("PORTUNUS_BACKEND", "mock")
    rc = main(["session", "list"])
    err = capsys.readouterr().err
    assert rc != 0
    assert "local" in err.lower()
