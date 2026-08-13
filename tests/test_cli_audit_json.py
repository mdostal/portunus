"""portunus audit --json [--secret SM_NAME] (story 06 prep): machine-readable
audit output for the UI's detail drawer. Metadata only -- audit entries never
carry a value, so this is safe to expose as-is."""
import json

from portunus.cli import main


def test_audit_json_outputs_a_list_of_entries(home, capsys):
    from portunus.audit import AuditChain
    a = AuditChain()
    a.append("resolve", "sm-a", "ok")
    a.append("resolve", "sm-b", "ok")

    rc = main(["audit", "--json"])
    out = capsys.readouterr().out

    assert rc == 0
    entries = json.loads(out)
    assert len(entries) == 2
    assert entries[0]["secret"] == "sm-a"


def test_audit_json_filters_by_secret(home, capsys):
    from portunus.audit import AuditChain
    a = AuditChain()
    a.append("resolve", "sm-a", "ok")
    a.append("resolve", "sm-b", "ok")
    a.append("adapter_resolution", "sm-a", "ok:env:X")

    rc = main(["audit", "--json", "--secret", "sm-a"])
    out = capsys.readouterr().out

    assert rc == 0
    entries = json.loads(out)
    assert len(entries) == 2
    assert all(e["secret"] == "sm-a" for e in entries)


def test_audit_json_respects_n_limit(home, capsys):
    from portunus.audit import AuditChain
    a = AuditChain()
    for i in range(5):
        a.append("resolve", "sm-a", "ok")

    rc = main(["audit", "2", "--json"])
    out = capsys.readouterr().out

    assert rc == 0
    entries = json.loads(out)
    assert len(entries) == 2


def test_audit_json_never_contains_a_value(home, capsys):
    from portunus.audit import AuditChain
    a = AuditChain()
    a.append("adapter_resolution", "sm-a", "ok:env:MY_VAR")

    rc = main(["audit", "--json"])
    out = capsys.readouterr().out
    assert "s3kr3t" not in out.lower()


def test_audit_plain_text_mode_still_works(home, capsys):
    from portunus.audit import AuditChain
    a = AuditChain()
    a.append("resolve", "sm-a", "ok")

    rc = main(["audit"])
    out = capsys.readouterr().out
    assert rc == 0
    assert "sm-a" in out
    # not JSON
    assert not out.strip().startswith("[")
