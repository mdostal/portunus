"""`portunus grant` only writes an audit record -- no IAM change. The CLI
output must say so plainly (PANT-853: it used to print `granted ... (audited)`,
which read like a real permission change)."""
from portunus import AuditChain, Registry
from portunus.cli import main


def test_grant_output_says_audit_only_and_no_iam_change(home, capsys):
    Registry().add("x", "dostal-x", state="enabled")

    rc = main(["grant", "x", "serviceAccount:agent-att@proj.iam"])
    out = capsys.readouterr().out

    assert rc == 0
    assert out.strip() == (
        "recorded grant serviceAccount:agent-att@proj.iam -> dostal-x "
        "in the audit log only -- no IAM change was made"
    )
    grants = [e for e in AuditChain().entries() if e["action"] == "grant"]
    assert grants and grants[-1]["result"] == "granted:serviceAccount:agent-att@proj.iam"


def test_grant_unknown_reference_fails(home, capsys):
    assert main(["grant", "nope", "someone@example.com"]) != 0
