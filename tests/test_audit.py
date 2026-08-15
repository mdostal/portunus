"""Audit hash-chain: intact on append, broken on tamper, monotonic seq."""
import os
import subprocess
import sys

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


def test_concurrent_appends_from_separate_processes_never_break_the_chain(home):
    """Real regression: two `portunus resolve` invocations racing on the real
    vault produced a duplicate seq and a broken hash chain in production --
    root cause was an unlocked read-modify-write on the sequence counter in
    append(). Reproduces the actual failure mode (separate OS processes, not
    just threads) rather than asserting the fix in the abstract."""
    barrier = home / "start-barrier"
    script = (
        "import time\n"
        "from pathlib import Path\n"
        "from portunus import AuditChain\n"
        f"barrier = Path({str(barrier)!r})\n"
        "deadline = time.monotonic() + 5\n"
        "while not barrier.exists() and time.monotonic() < deadline:\n"
        "    pass\n"  # tight spin, not sleep -- maximize simultaneity
        "AuditChain().append('resolve', 'dostal-race', 'ok')\n"
    )
    n = 8
    procs = [
        subprocess.Popen([sys.executable, "-c", script], env=os.environ.copy())
        for _ in range(n)
    ]
    barrier.write_text("go")  # release every spinning process at once
    for p in procs:
        assert p.wait() == 0

    a = AuditChain()
    entries = a.entries()
    assert len(entries) == n
    seqs = [e["seq"] for e in entries]
    assert sorted(seqs) == list(range(1, n + 1)), f"duplicate/missing seq: {seqs}"
    assert a.verify() is True
