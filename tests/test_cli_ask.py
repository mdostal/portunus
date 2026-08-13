"""portunus ask "<request>" (story 04): semantic front door end-to-end --
parse_intent -> resolve_by_tags -> boundary injection, fail-closed at every
layer, raw request text never written to the audit log."""
import json

import pytest

from portunus.audit import AuditChain
from portunus.cli import main

SECRET = "s3kr3t-do-not-leak-0xCAFE"


@pytest.fixture(autouse=True)
def _mock_backend(monkeypatch):
    monkeypatch.setenv("PORTUNUS_BACKEND", "mock")
    monkeypatch.setenv("PORTUNUS_MOCK_SM_VERCEL_MDOSTAL", SECRET)


def _register():
    from portunus import Registry
    reg = Registry()
    reg.add("vercel-mdostal", "sm-vercel-mdostal", provider="vercel", project="mdostal.com", env="prod")
    return reg


def test_ask_unambiguous_request_resolves_and_injects(home, capsys, monkeypatch):
    _register()
    monkeypatch.delenv("PORTUNUS_ASK_TEST", raising=False)

    rc = main(["ask", "the vercel secret for mdostal.com in prod",
               "--target", "env", "--var", "PORTUNUS_ASK_TEST"])
    captured = capsys.readouterr()

    assert rc == 0
    import os
    assert os.environ["PORTUNUS_ASK_TEST"] == SECRET
    assert SECRET not in captured.out
    assert SECRET not in captured.err


def test_ask_underspecified_request_fails_closed_on_resolve_ambiguity(home, capsys):
    from portunus import Registry
    reg = Registry()
    reg.add("prod-ref", "sm-prod", provider="vercel", project="mdostal.com", env="prod")
    reg.add("staging-ref", "sm-staging", provider="vercel", project="mdostal.com", env="staging")

    rc = main(["ask", "the vercel secret for mdostal.com", "--target", "env", "--var", "X"])
    err = capsys.readouterr().err

    assert rc != 0
    assert "prod-ref" in err and "staging-ref" in err


def test_ask_unrecognizable_request_fails_closed(home, capsys):
    _register()
    rc = main(["ask", "something totally unrelated", "--target", "env", "--var", "X"])
    err = capsys.readouterr().err
    assert rc != 0
    assert "specify" in err.lower() or "could not" in err.lower()


def test_ask_without_target_resolves_only_and_succeeds(home, capsys):
    """No --target is a preview, not a failure (story 06 prep: the UI's Ask
    Bar resolves before committing to an injection target)."""
    _register()
    rc = main(["ask", "the vercel secret for mdostal.com in prod", "--json"])
    out = capsys.readouterr().out

    assert rc == 0
    data = json.loads(out)
    assert data["name"] == "vercel-mdostal"
    assert SECRET not in out


def test_ask_never_writes_raw_request_text_to_audit(home):
    _register()
    request = "the vercel secret for mdostal.com in prod, please inject it now"
    main(["ask", request, "--target", "env", "--var", "PORTUNUS_ASK_TEST2"])

    audit = AuditChain()
    entries = [e for e in audit.entries() if e["action"] == "semantic_op"]
    assert entries
    for entry in entries:
        assert request not in json.dumps(entry)
        assert SECRET not in json.dumps(entry)
    assert audit.verify() is True


def test_ask_ambiguous_request_never_writes_raw_text_or_value(home):
    from portunus import Registry
    reg = Registry()
    reg.add("a", "sm-a", provider="vercel", project="mdostal.com")
    reg.add("b", "sm-b", provider="aws", project="other.com")

    request = "is it vercel or aws for this one"
    main(["ask", request, "--target", "env", "--var", "X"])

    audit = AuditChain()
    entries = [e for e in audit.entries() if e["action"] == "semantic_op"]
    assert entries
    assert request not in json.dumps(entries[0])
