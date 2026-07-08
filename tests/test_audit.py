"""Audit hash-chain: intact on append, broken on tamper, monotonic seq."""
from portunus import AuditChain


def test_chain_verifies_after_appends(home):
    a = AuditChain()
    a.append("resolve", "dostal-x", "ok")
    a.append("grant", "dostal-x", "granted:sa@x")
    a.append("resolve", "dostal-y", "ok")
    assert a.verify() is True
    seqs = [e["seq"] for e in a.entries()]
    assert seqs == [1, 2, 3]


def test_tamper_is_detected(home):
    a = AuditChain()
    a.append("resolve", "dostal-x", "ok")
    a.append("resolve", "dostal-y", "ok")
    lines = a.path.read_text().splitlines()
    # flip a result field in the first record without recomputing the hash
    lines[0] = lines[0].replace('"result":"ok"', '"result":"denied"')
    a.path.write_text("\n".join(lines) + "\n")
    assert AuditChain().verify() is False


def test_actor_and_task_from_env(home, monkeypatch):
    monkeypatch.setenv("DOSTAL_AGENT", "agent-att")
    monkeypatch.setenv("DOSTAL_TASK", "DOS-1")
    a = AuditChain()
    e = a.append("resolve", "dostal-x", "ok")
    assert e["actor"] == "agent-att"
    assert e["task"] == "DOS-1"
