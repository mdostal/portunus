"""Story 07 closeout: portunus verify against a single chain spanning every
audit entry type introduced across this epic (drop, semantic_op,
adapter_resolution, resolve), not just per-story in isolation."""
from portunus.audit import AuditChain
from portunus.cli import main

SECRET = "s3kr3t-do-not-leak-0xCAFE"


def test_full_chain_across_every_entry_type_verifies(home, monkeypatch):
    monkeypatch.setenv("PORTUNUS_BACKEND", "mock")
    monkeypatch.setenv("PORTUNUS_MOCK_SM_VERCEL_MDOSTAL", SECRET)

    # drop (with the story 01 tag schema)
    monkeypatch.delenv("PORTUNUS_BACKEND", raising=False)
    value_file = home / "value.txt"
    value_file.write_text(SECRET + "\n")
    main([
        "drop", "vercel-mdostal", "sm-vercel-mdostal", "--value-file", str(value_file),
        "--provider", "vercel", "--project", "mdostal.com", "--env", "prod",
    ])
    main(["state", "vercel-mdostal", "enabled"])

    # adapter_resolution (story 03) -- switch to mock backend so `inject`
    # can actually fetch a value without needing the local vault's key file.
    monkeypatch.setenv("PORTUNUS_BACKEND", "mock")
    main(["inject", "--tags", "provider=vercel,project=mdostal.com", "--target", "env", "--var", "X1"])

    # semantic_op, both resolve-only and injecting (story 04)
    main(["ask", "the vercel secret for mdostal.com in prod", "--json"])
    main(["ask", "the vercel secret for mdostal.com in prod", "--target", "env", "--var", "X2"])

    # semantic_op ambiguous-intent path
    main(["ask", "something totally unrelated"])

    audit = AuditChain()
    entries = audit.entries()
    actions = {e["action"] for e in entries}
    assert {"drop", "adapter_resolution", "semantic_op"} <= actions
    assert len(entries) >= 5

    assert audit.verify() is True

    for e in entries:
        assert SECRET not in str(e)
