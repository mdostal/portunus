"""portunus-petitio-rbac Story 03: portunus roles enforce on|off|status --
NotAuthorized actually raised, permissive-if-unconfigured posture unchanged.
Default: off. Enforcement marker lives under home() -- automatically
per-`--home`-scoped, zero extra threading needed."""
import pytest

from portunus import AuditChain, Broker, Registry
from portunus.broker import Identity, NotAuthorized
from portunus.roles import enforcement_is_on, set_enforcement, set_policy


def test_enforcement_defaults_to_off(home):
    assert enforcement_is_on() is False


def test_set_enforcement_on_and_off(home):
    set_enforcement(True)
    assert enforcement_is_on() is True
    set_enforcement(False)
    assert enforcement_is_on() is False


# --- default (off) behavior unchanged from Story 02 -------------------------

def test_enforcement_off_a_would_deny_decision_still_succeeds(home):
    reg = Registry()
    reg.add("x", "sm-x", org="firefly-events", state="enabled")
    broker = Broker(reg, AuditChain())
    set_policy("org", "firefly-events", "dev", ["read"], principal="claude-ffe")
    ref = broker.check_injectable("x", requester=Identity(name="codex-other", kind="agent"))
    assert ref.name == "x"  # enforcement never ran -- Story 02 behavior exactly


# --- enforcement on: real denial for a non-matching principal ---------------

def test_enforcement_on_raises_not_authorized_for_non_matching_principal(home):
    reg = Registry()
    reg.add("x", "sm-x", org="firefly-events", state="enabled")
    broker = Broker(reg, AuditChain())
    set_policy("org", "firefly-events", "dev", ["read"], principal="claude-ffe")
    set_enforcement(True)
    with pytest.raises(NotAuthorized):
        broker.check_injectable("x", requester=Identity(name="codex-other", kind="agent"))


def test_enforcement_on_still_allows_matching_principal(home):
    reg = Registry()
    reg.add("x", "sm-x", org="firefly-events", state="enabled")
    broker = Broker(reg, AuditChain())
    set_policy("org", "firefly-events", "dev", ["read"], principal="claude-ffe")
    set_enforcement(True)
    ref = broker.check_injectable("x", requester=Identity(name="claude-ffe", kind="agent"))
    assert ref.name == "x"


def test_enforcement_on_permissive_if_unconfigured_holds(home):
    """The core promise: enforcement being globally on never denies a
    reference whose OWN scope has zero configured policies."""
    reg = Registry()
    reg.add("x", "sm-x", org="firefly-events", state="enabled")
    reg.add("y", "sm-y", org="totally-unconfigured-org", state="enabled")
    broker = Broker(reg, AuditChain())
    set_policy("org", "firefly-events", "dev", ["read"], principal="claude-ffe")
    set_enforcement(True)
    # y's scope has no policy at all -- must succeed for ANY requester.
    ref = broker.check_injectable("y", requester=Identity(name="anyone-at-all", kind="agent"))
    assert ref.name == "y"


def test_enforcement_on_with_no_requester_never_raises(home):
    """requester=None is treated identically to no-policy-configured
    (roles.evaluate()'s own documented behavior) -- enforcement can't deny
    an anonymous caller it was never given an identity for."""
    reg = Registry()
    reg.add("x", "sm-x", org="firefly-events", state="enabled")
    broker = Broker(reg, AuditChain())
    set_policy("org", "firefly-events", "dev", ["read"], principal="claude-ffe")
    set_enforcement(True)
    ref = broker.check_injectable("x")
    assert ref.name == "x"


# --- NotAuthorized message never contains a secret value --------------------

def test_not_authorized_message_never_contains_a_value(home):
    reg = Registry()
    reg.add("x", "sm-x", org="firefly-events", state="enabled")
    broker = Broker(reg, AuditChain())
    set_policy("org", "firefly-events", "dev", ["read"], principal="claude-ffe")
    set_enforcement(True)
    with pytest.raises(NotAuthorized) as excinfo:
        broker.check_injectable("x", requester=Identity(name="codex-other", kind="agent"))
    message = str(excinfo.value)
    assert "sm-x" in message
    assert "codex-other" in message


# --- real per-`--home` isolation ---------------------------------------------

def test_enforcement_is_isolated_per_home(tmp_path, monkeypatch):
    home_a = tmp_path / "home-a"
    home_b = tmp_path / "home-b"
    # Pre-create B so it's an "existing" vault, not a brand-new one -- keeps
    # this test about isolation specifically, independent of Story 04's
    # separate new-vault-defaults-to-on behavior (see
    # test_enforcement_default_for_new_vaults.py for that).
    home_b.mkdir()

    monkeypatch.setenv("PORTUNUS_HOME", str(home_a))
    set_enforcement(True)
    assert enforcement_is_on() is True

    monkeypatch.setenv("PORTUNUS_HOME", str(home_b))
    assert enforcement_is_on() is False  # B's own (pre-existing, untouched) state, unaffected by A

    monkeypatch.setenv("PORTUNUS_HOME", str(home_a))
    assert enforcement_is_on() is True  # A's own state is untouched


# --- CLI ----------------------------------------------------------------------

def test_cli_roles_enforce_status_reports_off_by_default(home, capsys):
    from portunus.cli import main

    rc = main(["roles", "enforce", "status"])
    out = capsys.readouterr().out
    assert rc == 0
    assert "off" in out.lower()


def test_cli_roles_enforce_on_then_status(home, capsys):
    from portunus.cli import main

    main(["roles", "enforce", "on"])
    capsys.readouterr()
    rc = main(["roles", "enforce", "status"])
    out = capsys.readouterr().out
    assert rc == 0
    assert "on" in out.lower()


def test_cli_roles_enforce_writes_an_audit_entry(home, capsys):
    from portunus.cli import main

    main(["roles", "enforce", "on"])
    capsys.readouterr()
    entries = AuditChain().entries()
    assert any(e["action"] == "roles_config_changed" and "enforce" in e["result"] for e in entries)


def test_cli_roles_enforce_off_restores_default_behavior(home, capsys):
    from portunus.cli import main

    Registry().add("x", "sm-x", org="firefly-events", state="enabled")
    main(["roles", "set", "--scope-type", "org", "--scope-value", "firefly-events", "--role", "dev", "--principal", "claude-ffe"])
    main(["roles", "enforce", "on"])
    main(["roles", "enforce", "off"])
    capsys.readouterr()

    broker = Broker(Registry(), AuditChain())
    ref = broker.check_injectable("x", requester=Identity(name="codex-other", kind="agent"))
    assert ref.name == "x"


# --- resolve/sync/leak-scan callers must skip gracefully, never crash -------
# Real bug found while smoke-testing this story: NotAuthorized wasn't caught
# anywhere NotInjectable/ApprovalRequired already were -- cmd_resolve would
# have shown a raw traceback instead of a clean error, and worse, cmd_sync/
# portunus_sync/get_values (leakscan) would have CRASHED THE WHOLE LOOP on
# the first denied reference instead of skipping it like the other two
# exceptions already do.

def test_cmd_resolve_reports_not_authorized_cleanly_not_a_traceback(home, monkeypatch, capsys):
    from portunus.cli import main

    Registry().add("x", "sm-x", org="firefly-events", state="enabled")
    set_policy("org", "firefly-events", "dev", ["read"], principal="claude-ffe")
    set_enforcement(True)
    monkeypatch.setenv("DOSTAL_AGENT", "codex-other")

    rc = main(["resolve", "--exec", "echo", "{{secret:x}}"])
    captured = capsys.readouterr()
    assert rc != 0
    assert "not authorized" in captured.err.lower()


def test_cmd_sync_skips_a_denied_reference_instead_of_crashing(home, monkeypatch, capsys):
    from portunus.cli import main

    Registry().add("x", "sm-x", org="firefly-events", project="myproj", state="enabled")
    set_policy("org", "firefly-events", "dev", ["read"], principal="claude-ffe")
    set_enforcement(True)
    monkeypatch.setenv("DOSTAL_AGENT", "codex-other")

    rc = main(["sync", "myproj", "--json"])  # must not raise/crash
    out = capsys.readouterr().out
    assert rc == 0
    payload = out.strip()
    assert payload  # produced real output rather than dying mid-loop


def test_mcp_sync_skips_a_denied_reference_instead_of_crashing(home, monkeypatch):
    from portunus.mcp_server import portunus_sync

    Registry().add("x", "sm-x", org="firefly-events", project="myproj", state="enabled")
    set_policy("org", "firefly-events", "dev", ["read"], principal="claude-ffe")
    set_enforcement(True)
    monkeypatch.setenv("DOSTAL_AGENT", "codex-other")

    result = portunus_sync("myproj")  # must not raise
    assert "synced" in result and "already_fresh" in result and "failed" in result


def test_get_values_skips_a_denied_reference_instead_of_crashing(home, monkeypatch):
    from portunus.leakscan import get_values

    reg = Registry()
    reg.add("x", "sm-x", org="firefly-events", state="enabled")
    audit = AuditChain()
    broker = Broker(reg, audit)
    backend = __import__("portunus").MockBackend()
    backend.set("sm-x", "FAKE-TEST-VALUE-do-not-leak-0xF00D")
    set_policy("org", "firefly-events", "dev", ["read"], principal="claude-ffe")
    set_enforcement(True)
    monkeypatch.setenv("DOSTAL_AGENT", "codex-other")

    values = get_values(reg, broker, backend)  # must not raise
    assert values == {}  # the denied reference contributes nothing, silently
