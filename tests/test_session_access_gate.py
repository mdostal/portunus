"""portunus-session-access-gate Story 01: Broker.check_session_access() --
gates `session load` through the exact same roles.evaluate() seam
check_injectable() already uses. A synthetic, in-memory Reference (never
persisted to the Registry) carries the session's own org/project/env/repo
scope metadata; roles.evaluate() is fully duck-typed against it. Only the
fetch boundary (session load) is gated -- store/remove/inspect/list stay
ungated, mirroring check_injectable's own write/metadata-view exemption."""
import json

import pytest

from portunus import AuditChain, Broker, Registry
from portunus.broker import Identity, NotAuthorized
from portunus.cli import main
from portunus.localvault import LocalEncryptedBackend
from portunus.roles import set_enforcement, set_policy

SESSION_SECRET = "SESSION-COOKIE-do-not-leak-0xDEAD"


def _session_json():
    return json.dumps({"cookies": [{"name": "sessionid", "value": SESSION_SECRET}]})


@pytest.fixture(autouse=True)
def _local_backend(monkeypatch):
    monkeypatch.delenv("PORTUNUS_BACKEND", raising=False)


# --- Broker.check_session_access() -- unit-level, direct -------------------

def test_check_session_access_permissive_when_no_policies_configured(home):
    broker = Broker(Registry(), AuditChain())
    # Must not raise.
    broker.check_session_access(
        "example.test", "user", org="firefly-events",
        requester=Identity(name="anyone", kind="agent"),
    )


def test_check_session_access_none_requester_never_raises(home):
    broker = Broker(Registry(), AuditChain())
    set_policy("org", "firefly-events", "dev", ["read"], principal="claude-ffe")
    set_enforcement(True)
    # Fail-open at the identity layer, mirrors check_injectable exactly.
    broker.check_session_access("example.test", "user", org="firefly-events", requester=None)


def test_check_session_access_would_deny_audited_but_not_blocked_when_enforcement_off(home):
    broker = Broker(Registry(), AuditChain())
    set_policy("org", "firefly-events", "dev", ["read"], principal="claude-ffe")
    # enforcement stays off (default)
    broker.check_session_access(
        "example.test", "user", org="firefly-events",
        requester=Identity(name="codex-other", kind="agent"),
    )  # must not raise
    audit_text = (home / "audit.log").read_text()
    assert "would-deny" in audit_text


def test_check_session_access_raises_not_authorized_when_enforcement_on_and_denied(home):
    broker = Broker(Registry(), AuditChain())
    set_policy("org", "firefly-events", "dev", ["read"], principal="claude-ffe")
    set_enforcement(True)
    with pytest.raises(NotAuthorized):
        broker.check_session_access(
            "example.test", "user", org="firefly-events",
            requester=Identity(name="codex-other", kind="agent"),
        )


def test_check_session_access_allows_matching_principal_when_enforcement_on(home):
    broker = Broker(Registry(), AuditChain())
    set_policy("org", "firefly-events", "dev", ["read"], principal="claude-ffe")
    set_enforcement(True)
    broker.check_session_access(
        "example.test", "user", org="firefly-events",
        requester=Identity(name="claude-ffe", kind="agent"),
    )  # must not raise


@pytest.mark.parametrize("scope_type", ["org", "project", "env", "repo"])
def test_check_session_access_matches_every_valid_scope_type(home, scope_type):
    broker = Broker(Registry(), AuditChain())
    set_policy(scope_type, "the-value", "dev", ["read"], principal="claude-ffe")
    set_enforcement(True)
    kwargs = {scope_type: "the-value"}
    with pytest.raises(NotAuthorized):
        broker.check_session_access(
            "example.test", "user", requester=Identity(name="someone-else", kind="agent"), **kwargs
        )
    # A matching principal is still allowed for the same scope type.
    broker.check_session_access(
        "example.test", "user", requester=Identity(name="claude-ffe", kind="agent"), **kwargs
    )


def test_check_session_access_never_creates_a_registry_entry(home):
    reg = Registry()
    broker = Broker(reg, AuditChain())
    set_policy("org", "firefly-events", "dev", ["read"], principal="claude-ffe")
    set_enforcement(True)
    broker.check_session_access(
        "example.test", "user", org="firefly-events",
        requester=Identity(name="claude-ffe", kind="agent"),
    )
    assert len(list(reg)) == 0
    assert "session:example.test:user" not in reg


# --- CLI: only `session load` is gated --------------------------------------

def _store(home, ttl_seconds=3600, site="example.test", account="dostal@example.test", org=""):
    value_file = home / "session.json"
    value_file.write_text(_session_json())
    args = ["session", "store", site, account, "--value-file", str(value_file),
            "--ttl-seconds", str(ttl_seconds)]
    if org:
        args += ["--org", org]
    return main(args)


def test_session_load_unchanged_when_no_scope_and_no_policies(home, capsys):
    _store(home)
    capsys.readouterr()
    rc = main(["session", "load", "example.test", "dostal@example.test"])
    captured = capsys.readouterr()
    assert rc == 0
    path = captured.out.strip()
    import os
    os.unlink(path)


def test_session_load_blocked_when_scoped_denied_and_enforcement_on(home, capsys, monkeypatch):
    monkeypatch.setenv("DOSTAL_AGENT", "codex-other")
    _store(home, org="firefly-events")
    capsys.readouterr()
    set_policy("org", "firefly-events", "dev", ["read"], principal="claude-ffe")
    set_enforcement(True)
    rc = main(["session", "load", "example.test", "dostal@example.test"])
    err = capsys.readouterr().err
    assert rc != 0
    assert "not authorized" in err.lower()
    assert SESSION_SECRET not in err


def test_session_load_allowed_when_scoped_and_matching_principal(home, capsys, monkeypatch):
    monkeypatch.setenv("DOSTAL_AGENT", "claude-ffe")
    _store(home, org="firefly-events")
    capsys.readouterr()
    set_policy("org", "firefly-events", "dev", ["read"], principal="claude-ffe")
    set_enforcement(True)
    rc = main(["session", "load", "example.test", "dostal@example.test"])
    captured = capsys.readouterr()
    assert rc == 0
    import os
    os.unlink(captured.out.strip())


@pytest.mark.parametrize("action,args", [
    ("store", None),  # handled specially below
    ("inspect", ["session", "inspect", "example.test", "dostal@example.test"]),
    ("list", ["session", "list"]),
    ("remove", ["session", "remove", "example.test", "dostal@example.test"]),
])
def test_non_load_session_commands_are_never_gated(home, capsys, monkeypatch, action, args):
    monkeypatch.setenv("DOSTAL_AGENT", "codex-other")
    _store(home, org="firefly-events")
    capsys.readouterr()
    set_policy("org", "firefly-events", "dev", ["read"], principal="claude-ffe")
    set_enforcement(True)
    if action == "store":
        rc = _store(home, site="another.test", account="a", org="firefly-events")
    else:
        rc = main(args)
    # None of these are gated -- a denying policy + enforcement on never blocks them.
    assert rc == 0


def test_session_scope_stored_and_backward_compatible_with_scope_less_record(home):
    backend = LocalEncryptedBackend()
    backend.store_session("a.test", "u", {"cookies": []}, ttl_seconds=60, org="firefly-events")
    view = backend.inspect_session("a.test", "u")
    assert view["scope"]["org"] == "firefly-events"

    # An old, scope-less record (pre-this-story) must still load cleanly.
    record = {
        "schema": "portunus.session.v1",
        "namespace": {"site": "old.test", "account": "u"},
        "ttl": {"seconds": 60, "expires_at": "2099-01-01T00:00:00Z"},
        "rotation": {"generation": 1, "last_rotated_at": "2026-01-01T00:00:00Z",
                     "interval_seconds": None, "rotate_after": None},
        "session": {"cookies": []},
    }
    backend.store(backend.session_key("old.test", "u"), json.dumps(record))
    old_view = backend.inspect_session("old.test", "u")
    assert old_view["scope"] == {"org": "", "project": "", "env": "", "repo": ""}
