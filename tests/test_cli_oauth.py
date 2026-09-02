"""portunus-oauth-token-broker Story 02: `portunus oauth store|list|remove` --
store/load/list/remove an OAuth credential bundle (client_id, client_secret,
refresh_token, token_endpoint). Mirrors `session store`'s exact stdin-only-in
discipline; TTL-free (unlike sessions) since the provider itself is the real
authority on refresh-token validity, not a client-side guess -- see
design-discussion.md §2."""
import json

import pytest

from portunus.audit import AuditChain
from portunus.cli import main
from portunus.localvault import OAUTH_CREDENTIAL_SCHEMA, LocalEncryptedBackend

CLIENT_SECRET = "CLIENT-SECRET-do-not-leak-0xDEAD"
REFRESH_TOKEN = "REFRESH-TOKEN-do-not-leak-0xBEEF"


def _credential_json():
    return json.dumps({
        "client_id": "client-123.apps.googleusercontent.com",
        "client_secret": CLIENT_SECRET,
        "refresh_token": REFRESH_TOKEN,
        "token_endpoint": "https://oauth2.googleapis.com/token",
    })


@pytest.fixture(autouse=True)
def _local_backend(monkeypatch):
    monkeypatch.delenv("PORTUNUS_BACKEND", raising=False)


def _store(home, provider="google", account="user@example.com"):
    value_file = home / "credential.json"
    value_file.write_text(_credential_json())
    rc = main(["oauth", "store", provider, account, "--value-file", str(value_file)])
    return rc


def test_oauth_store_confirms_by_namespace_only(home, capsys):
    rc = _store(home)
    captured = capsys.readouterr()
    assert rc == 0
    assert "google" in captured.out
    assert "user@example.com" in captured.out
    assert CLIENT_SECRET not in captured.out
    assert REFRESH_TOKEN not in captured.out
    assert CLIENT_SECRET not in captured.err
    assert REFRESH_TOKEN not in captured.err


def test_oauth_store_malformed_json_fails_closed(home, capsys):
    value_file = home / "bad.json"
    value_file.write_text("not valid json{{{")
    rc = main(["oauth", "store", "google", "user@example.com", "--value-file", str(value_file)])
    err = capsys.readouterr().err
    assert rc != 0
    assert "json" in err.lower() or "invalid" in err.lower()


def test_oauth_store_requires_stdin_or_value_file(home, capsys):
    with pytest.raises(SystemExit):
        main(["oauth", "store", "google", "user@example.com"])


def test_oauth_round_trips_through_the_real_backend(home):
    _store(home)
    backend = LocalEncryptedBackend()
    record = backend.load_oauth_credential("google", "user@example.com")
    assert record["schema"] == OAUTH_CREDENTIAL_SCHEMA
    assert record["credential"]["client_secret"] == CLIENT_SECRET
    assert record["credential"]["refresh_token"] == REFRESH_TOKEN


def test_oauth_credential_never_written_to_disk_in_plaintext(home):
    _store(home)
    for name in ("vault.enc.json", "registry.json", "audit.log"):
        path = home / name
        if path.exists():
            text = path.read_text()
            assert CLIENT_SECRET not in text
            assert REFRESH_TOKEN not in text


def test_oauth_list_shows_metadata_only_never_credential_fields(home, capsys):
    _store(home, provider="google", account="a@example.com")
    _store(home, provider="github", account="b@example.com")
    capsys.readouterr()
    rc = main(["oauth", "list", "--json"])
    out = capsys.readouterr().out
    assert rc == 0
    data = json.loads(out)
    assert len(data) == 2
    assert CLIENT_SECRET not in out
    assert REFRESH_TOKEN not in out
    providers = {d["namespace"]["provider"] for d in data}
    assert providers == {"google", "github"}


def test_oauth_remove_confirms_by_namespace_and_actually_removes(home, capsys):
    _store(home)
    rc = main(["oauth", "remove", "google", "user@example.com"])
    captured = capsys.readouterr()
    assert rc == 0
    assert "google" in captured.out

    backend = LocalEncryptedBackend()
    with pytest.raises(Exception):
        backend.load_oauth_credential("google", "user@example.com")


def test_oauth_remove_not_found_reports_cleanly(home, capsys):
    rc = main(["oauth", "remove", "google", "does-not-exist@example.com"])
    err = capsys.readouterr().err
    assert rc != 0
    assert "no such" in err.lower() or "not found" in err.lower()


def test_oauth_store_and_remove_write_audit_entries_list_does_not(home, capsys):
    _store(home)
    capsys.readouterr()
    main(["oauth", "list"])
    capsys.readouterr()
    main(["oauth", "remove", "google", "user@example.com"])
    capsys.readouterr()

    audit = AuditChain()
    actions = [e["action"] for e in audit.entries()]
    assert actions.count("oauth_store") == 1
    assert actions.count("oauth_remove") == 1
    assert "oauth_list" not in actions
    for e in audit.entries():
        assert CLIENT_SECRET not in json.dumps(e)
        assert REFRESH_TOKEN not in json.dumps(e)
    assert audit.verify() is True


def test_oauth_commands_require_local_backend(home, capsys, monkeypatch):
    monkeypatch.setenv("PORTUNUS_BACKEND", "mock")
    rc = main(["oauth", "list"])
    err = capsys.readouterr().err
    assert rc != 0
    assert "local" in err.lower()


def test_oauth_and_session_namespaces_do_not_collide(home):
    """oauth: and session: are distinct key prefixes -- storing an oauth
    credential and a session under the SAME provider/account-shaped
    namespace must not shadow or overwrite each other."""
    _store(home, provider="example.test", account="dostal@example.test")
    backend = LocalEncryptedBackend()
    backend.store_session(
        "example.test", "dostal@example.test", {"cookies": []}, ttl_seconds=3600,
    )
    # Both readable independently -- neither one clobbered the other.
    oauth_record = backend.load_oauth_credential("example.test", "dostal@example.test")
    assert oauth_record["credential"]["client_secret"] == CLIENT_SECRET
    session_record = backend.load_session("example.test", "dostal@example.test")
    assert session_record["session"] == {"cookies": []}


# --- direct backend-level tests (list/remove not-found, TTL-free shape) ------

def test_list_oauth_credentials_empty_when_none_stored(home):
    backend = LocalEncryptedBackend()
    assert backend.list_oauth_credentials() == []


def test_remove_oauth_credential_returns_false_when_not_found(home):
    backend = LocalEncryptedBackend()
    assert backend.remove_oauth_credential("google", "nobody@example.com") is False


def test_oauth_credential_record_has_no_ttl_field(home):
    """Unlike sessions, an oauth credential record carries no client-side
    TTL/rotation guess -- the provider's own refresh grant is the real
    authority on validity (design-discussion.md §2 self-grill)."""
    backend = LocalEncryptedBackend()
    backend.store_oauth_credential("google", "user@example.com", json.loads(_credential_json()))
    record = backend.load_oauth_credential("google", "user@example.com")
    assert "ttl" not in record
    assert "rotation" not in record
