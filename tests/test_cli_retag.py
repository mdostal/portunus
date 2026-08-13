"""portunus retag <name> --provider/--project/--env/--tags (story 04,
portunus-agent-ops-federation). Metadata-only, mirrors the exit-code
convention already established by find/inject (EXIT_NO_MATCH/EXIT_AMBIGUOUS)."""
import json

from portunus import Registry
from portunus.audit import AuditChain
from portunus.cli import main


def test_retag_updates_and_prints_new_tags(home, capsys):
    reg = Registry()
    reg.add("x", "sm-x", provider="vercel", project="mdostal.com", env="staging")

    rc = main(["retag", "x", "--env", "prod"])
    out = capsys.readouterr().out
    assert rc == 0
    assert "prod" in out

    reloaded = Registry().require("x")
    assert reloaded.env == "prod"


def test_retag_collision_fails_with_distinct_exit_code_naming_collider(home, capsys):
    reg = Registry()
    reg.add("a", "sm-a", provider="vercel", project="mdostal.com", env="prod")
    reg.add("b", "sm-b", provider="vercel", project="mdostal.com", env="staging")

    rc = main(["retag", "b", "--env", "prod"])
    err = capsys.readouterr().err
    assert rc != 0
    assert "a" in err

    # distinct from EXIT_NO_MATCH (3) used by find/inject for "no match"
    rc_no_match = main(["retag", "nonexistent", "--env", "prod"])
    assert rc_no_match != 0
    assert rc_no_match != rc  # ambiguous vs unknown-name are different failure modes


def test_retag_writes_audit_entry_and_verify_passes(home):
    reg = Registry()
    reg.add("x", "sm-x", provider="vercel", project="mdostal.com", env="staging")
    main(["retag", "x", "--env", "prod"])

    audit = AuditChain()
    entries = [e for e in audit.entries() if e["action"] == "retag"]
    assert len(entries) == 1
    assert entries[0]["secret"] == "sm-x"
    assert audit.verify() is True


def test_retag_never_bypasses_the_registry_collision_check(home, capsys):
    """Regression guard: retag must call Registry.retag(), not a hand-rolled
    CLI-level tag merge that could drift from the collision logic."""
    reg = Registry()
    reg.add("a", "sm-a", provider="vercel", project="mdostal.com", env="prod")
    reg.add("b", "sm-b", provider="vercel", project="mdostal.com", env="staging")
    rc = main(["retag", "b", "--provider", "vercel", "--project", "mdostal.com", "--env", "prod"])
    capsys.readouterr()
    assert rc != 0
    reloaded = Registry().require("b")
    assert reloaded.env == "staging"  # unchanged -- collision rejected, no partial write


def test_retag_tags_flag(home, capsys):
    reg = Registry()
    reg.add("x", "sm-x", tags={"team": "platform"})
    rc = main(["retag", "x", "--tags", "team=growth"])
    capsys.readouterr()
    assert rc == 0
    reloaded = Registry().require("x")
    assert reloaded.tags == {"team": "growth"}
