"""`portunus metadata confirm/reject/pending` (portunus-vault-trust-and-
access Slice 6) -- the human-review counterpart to portunus_suggest_metadata."""
import json

from portunus import AuditChain, Registry
from portunus.cli import main


def test_metadata_pending_lists_suggestions(home, capsys):
    reg = Registry()
    reg.add("x", "sm-x")
    reg.suggest_metadata("x", by="claude-code", fields={"description": "a stripe key"})

    rc = main(["metadata", "pending", "--json"])
    out = capsys.readouterr().out
    assert rc == 0
    data = json.loads(out)
    assert data["x"]["description"]["value"] == "a stripe key"
    assert data["x"]["description"]["by"] == "claude-code"


def test_metadata_confirm_applies_and_clears(home, capsys):
    reg = Registry()
    reg.add("x", "sm-x")
    reg.suggest_metadata("x", by="claude-code", fields={"description": "a stripe key"})
    capsys.readouterr()

    rc = main(["metadata", "confirm", "x", "description"])
    out = capsys.readouterr().out
    assert rc == 0
    assert "confirmed" in out

    ref = Registry().require("x")
    assert ref.description == "a stripe key"
    assert "description" not in ref.suggested


def test_metadata_reject_clears_without_applying(home, capsys):
    reg = Registry()
    reg.add("x", "sm-x")
    reg.suggest_metadata("x", by="claude-code", fields={"description": "a stripe key"})
    capsys.readouterr()

    rc = main(["metadata", "reject", "x", "description"])
    out = capsys.readouterr().out
    assert rc == 0
    assert "rejected" in out

    ref = Registry().require("x")
    assert ref.description == ""
    assert "description" not in ref.suggested


def test_metadata_confirm_with_no_pending_suggestion_fails_clearly(home, capsys):
    Registry().add("x", "sm-x")
    rc = main(["metadata", "confirm", "x", "description"])
    err = capsys.readouterr().err
    assert rc != 0
    assert "no pending suggestion" in err


def test_metadata_confirm_writes_audit_entry(home, capsys):
    reg = Registry()
    reg.add("x", "sm-x")
    reg.suggest_metadata("x", by="claude-code", fields={"purpose": "billing"})
    capsys.readouterr()

    main(["metadata", "confirm", "x", "purpose"])
    capsys.readouterr()
    entries = AuditChain().entries()
    assert any(e["action"] == "metadata_confirmed" for e in entries)


def test_metadata_reject_writes_audit_entry(home, capsys):
    reg = Registry()
    reg.add("x", "sm-x")
    reg.suggest_metadata("x", by="claude-code", fields={"purpose": "billing"})
    capsys.readouterr()

    main(["metadata", "reject", "x", "purpose"])
    capsys.readouterr()
    entries = AuditChain().entries()
    assert any(e["action"] == "metadata_rejected" for e in entries)


def test_metadata_field_argument_rejects_routing_fields():
    """argparse's own choices= restricts `field` to SUGGESTIBLE_FIELDS --
    "project"/"org"/etc aren't even parseable here."""
    import pytest

    with pytest.raises(SystemExit):
        main(["metadata", "confirm", "x", "project"])
