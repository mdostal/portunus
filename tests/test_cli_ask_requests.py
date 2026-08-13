"""portunus ask routing for add/rotate intents (story 03,
portunus-agent-ops-federation). The agent never supplies or sees a value at
any point in this flow -- fulfillment still requires a human running
`portunus drop` or using the UI's add-secret form."""
import json

import pytest

from portunus import Registry
from portunus.audit import AuditChain
from portunus.cli import main

SECRET = "s3kr3t-do-not-leak-0xCAFE"


@pytest.fixture(autouse=True)
def _mock_backend(monkeypatch):
    monkeypatch.setenv("PORTUNUS_BACKEND", "mock")
    monkeypatch.setenv("PORTUNUS_MOCK_SM_VERCEL_MDOSTAL", SECRET)


def _register():
    reg = Registry()
    reg.add("vercel-mdostal", "sm-vercel-mdostal", provider="vercel", project="mdostal.com", env="prod")
    return reg


# --- regression: fetch path byte-identical to before this story -----------

def test_fetch_intent_still_resolves_and_injects_unchanged(home, capsys, monkeypatch):
    _register()
    monkeypatch.delenv("PORTUNUS_ASK_REQ_TEST", raising=False)
    rc = main(["ask", "the vercel secret for mdostal.com in prod",
               "--target", "env", "--var", "PORTUNUS_ASK_REQ_TEST"])
    captured = capsys.readouterr()
    assert rc == 0
    import os
    assert os.environ["PORTUNUS_ASK_REQ_TEST"] == SECRET
    assert SECRET not in captured.out and SECRET not in captured.err


# --- add intent -------------------------------------------------------

def test_add_intent_requires_explicit_name_and_tags(home, capsys):
    _register()
    rc = main(["ask", "please add a new secret for github ci"])
    err = capsys.readouterr().err
    assert rc != 0
    assert "--name" in err and "--tags" in err


def test_add_intent_with_name_and_tags_creates_requested_placeholder(home, capsys):
    _register()
    rc = main(["ask", "add a new secret", "--name", "gh-ci-token",
               "--tags", "provider=github,project=portunus,env=prod"])
    out = capsys.readouterr().out
    assert rc == 0
    assert "requested" in out.lower()

    ref = Registry().require("gh-ci-token")
    assert ref.state == "requested"
    assert ref.provider == "github"


def test_add_intent_writes_requested_add_audit_entry_never_raw_text(home):
    _register()
    request_text = "add a new secret please, this is sensitive context"
    main(["ask", request_text, "--name", "gh-ci-token",
          "--tags", "provider=github,project=portunus,env=prod"])

    audit = AuditChain()
    entries = [e for e in audit.entries() if e["action"] == "semantic_op"]
    assert any("requested:add" in e["result"] for e in entries)
    for e in entries:
        assert request_text not in json.dumps(e)
    assert audit.verify() is True


# --- rotate intent ------------------------------------------------------

def test_rotate_intent_flags_existing_reference_without_touching_value(home, capsys):
    _register()
    rc = main(["ask", "rotate the vercel secret for mdostal.com in prod"])
    out = capsys.readouterr().out
    assert rc == 0
    assert "rotat" in out.lower()

    ref = Registry().require("vercel-mdostal")
    assert ref.tags.get("rotation_requested") == "true"
    assert ref.state == "enabled"  # untouched -- still injectable, value unaffected


def test_rotate_intent_no_match_fails_closed_like_fetch(home, capsys):
    _register()
    rc = main(["ask", "rotate the aws secret for nope.example"])
    err = capsys.readouterr().err
    assert rc != 0
    # Same fail-closed resolution path fetch uses (parse_intent /
    # resolve_by_tags) -- there is nothing to rotate if it can't be found.
    assert "no" in err.lower()


def test_rotate_intent_writes_requested_rotate_audit_entry(home):
    _register()
    main(["ask", "rotate the vercel secret for mdostal.com in prod"])
    audit = AuditChain()
    entries = [e for e in audit.entries() if e["action"] == "semantic_op"]
    assert any("requested:rotate" in e["result"] for e in entries)
    assert audit.verify() is True


def test_rotate_intent_never_leaks_value_in_output(home, capsys):
    _register()
    rc = main(["ask", "rotate the vercel secret for mdostal.com in prod"])
    captured = capsys.readouterr()
    assert SECRET not in captured.out and SECRET not in captured.err
