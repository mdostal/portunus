"""`portunus roles set/delete/show` -- STUB ONLY (portunus-vault-trust-and-
access Slice 5). Writes genuinely persist; nothing enforces them."""
import json

import pytest

from portunus import AuditChain, Broker, Registry
from portunus.cli import main


def test_roles_set_and_show(home, capsys):
    rc = main([
        "roles", "set", "--scope-type", "org", "--scope-value", "firefly-events",
        "--role", "dev", "--actions", "read,test",
    ])
    assert rc == 0
    capsys.readouterr()

    rc = main(["roles", "show", "--json"])
    out = capsys.readouterr().out
    assert rc == 0
    data = json.loads(out)
    key = "org:firefly-events:dev"
    assert data[key]["actions"] == ["read", "test"]


def test_roles_show_warns_it_is_a_stub(home, capsys):
    main([
        "roles", "set", "--scope-type", "project", "--scope-value", "shindig",
        "--role", "admin", "--actions", "read,test,prod-release",
    ])
    capsys.readouterr()

    rc = main(["roles", "show"])
    out = capsys.readouterr().out
    assert rc == 0
    assert "STUB" in out.upper()


def test_roles_delete(home, capsys):
    main(["roles", "set", "--scope-type", "org", "--scope-value", "firefly-events", "--role", "dev"])
    capsys.readouterr()

    rc = main(["roles", "delete", "--scope-type", "org", "--scope-value", "firefly-events", "--role", "dev"])
    out = capsys.readouterr().out
    assert rc == 0
    assert "deleted" in out


def test_roles_set_rejects_invalid_scope_type(home, capsys):
    # argparse's own choices= catches this before set_policy() is ever
    # called -- SystemExit(2), the standard argparse invalid-choice path.
    with pytest.raises(SystemExit):
        main(["roles", "set", "--scope-type", "bogus", "--scope-value", "x", "--role", "dev"])


def test_roles_set_writes_a_config_audit_entry(home, capsys):
    main(["roles", "set", "--scope-type", "org", "--scope-value", "firefly-events", "--role", "dev"])
    capsys.readouterr()
    entries = AuditChain().entries()
    assert any(e["action"] == "roles_config_changed" for e in entries)


def test_check_injectable_unaffected_by_cli_configured_roles(home, capsys):
    """End-to-end proof at the CLI layer: configuring restrictive-looking
    policies through the real command never changes check_injectable's
    real, unrelated decision."""
    Registry().add("x", "sm-x", org="firefly-events", project="shindig", state="enabled")
    main(["roles", "set", "--scope-type", "project", "--scope-value", "shindig", "--role", "viewer", "--actions", ""])
    capsys.readouterr()

    broker = Broker(Registry(), AuditChain())
    ref = broker.check_injectable("x")
    assert ref.name == "x"
