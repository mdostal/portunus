"""PANT-851: the audit seq never resets when .clock is lost, and a corrupt
log line is reported by verify() -- never raised."""
import pytest

from portunus import AuditChain, Broker, Registry
from portunus.audit import verify_entries
from portunus.broker import ApprovalRequired
from portunus.cli import main


def _three(a):
    for s in ("x", "y", "z"):
        a.append("resolve", f"dostal-{s}", "ok")


@pytest.mark.parametrize("clock_state", ["missing", "garbage", "empty", "rolled-back"])
def test_seq_recovers_from_log_when_clock_is_lost(home, clock_state):
    a = AuditChain()
    _three(a)
    if clock_state == "missing":
        a.clock_path.unlink()
    elif clock_state == "garbage":
        a.clock_path.write_text("not-a-number")
    elif clock_state == "empty":
        a.clock_path.write_text("")
    else:
        a.clock_path.write_text("1")

    e = AuditChain().append("resolve", "dostal-w", "ok")

    assert e["seq"] == 4
    assert a.clock_path.read_text() == "4"  # .clock rewritten
    assert [x["seq"] for x in a.entries()] == [1, 2, 3, 4]
    assert a.verify() is True


def test_seq_still_starts_at_one_on_an_empty_log(home):
    assert AuditChain().append("resolve", "dostal-x", "ok")["seq"] == 1


def test_seq_recovery_skips_corrupt_lines(home):
    a = AuditChain()
    _three(a)
    with a.path.open("a") as fh:
        fh.write("{not json\n")
    a.clock_path.unlink()
    assert a.append("resolve", "dostal-w", "ok")["seq"] == 4


def test_corrupt_line_makes_verify_false_not_raise(home):
    a = AuditChain()
    _three(a)
    lines = a.path.read_text().splitlines()
    lines[1] = lines[1][:20]  # truncate line 2 mid-JSON
    a.path.write_text("\n".join(lines) + "\n")

    assert a.verify() is False
    result = a.check()
    assert result["ok"] is False
    assert result["line"] == 2
    assert "unparseable" in result["reason"]
    assert result["entries"] == 1
    assert [e["seq"] for e in a.entries()] == [1, 3]  # entries() skips, doesn't raise


@pytest.mark.parametrize("bad", ['[1,2]', '"str"', '{"seq":1}', '{"seq":1,"actor":"a","task":"",'
                                 '"action":"r","secret":"s","result":"ok","prev":null}'])
def test_malformed_entry_is_reported_not_raised(home, bad):
    a = AuditChain()
    a.append("resolve", "dostal-x", "ok")
    with a.path.open("a") as fh:
        fh.write(bad + "\n")
    result = a.check()
    assert result == {"ok": False, "entries": 1, "line": 2, "reason": result["reason"]}
    assert result["reason"]


def test_verify_entries_never_raises_on_malformed_entries():
    assert verify_entries([{"seq": 1}]) is False
    assert verify_entries(["nope"]) is False
    assert verify_entries([]) is True


def test_tamper_reports_the_modified_line(home):
    a = AuditChain()
    _three(a)
    lines = a.path.read_text().splitlines()
    lines[2] = lines[2].replace('"result":"ok"', '"result":"denied"')
    a.path.write_text("\n".join(lines) + "\n")
    result = a.check()
    assert (result["ok"], result["line"]) == (False, 3)
    assert "hash mismatch" in result["reason"]


def test_cli_verify_names_the_corrupt_line_and_exits_nonzero(home, capsys):
    a = AuditChain()
    _three(a)
    lines = a.path.read_text().splitlines()
    lines[1] = "garbage"
    a.path.write_text("\n".join(lines) + "\n")

    rc = main(["verify"])
    out = capsys.readouterr().out
    assert rc == 2
    assert "BROKEN at line 2" in out


def test_cli_verify_intact(home, capsys):
    _three(AuditChain())
    assert main(["verify"]) == 0
    assert "INTACT (3 entries)" in capsys.readouterr().out


def test_cli_audit_listing_survives_a_corrupt_line(home, capsys):
    a = AuditChain()
    _three(a)
    with a.path.open("a") as fh:
        fh.write("garbage\n")
    assert main(["audit"]) == 0


def _gated_broker():
    reg = Registry()
    reg.add("x", "dostal-x", scope="shared", kind="anthropic")
    b = Broker(reg, AuditChain())
    b.gate("x", on=True)
    return b


def test_clock_now_recovers_from_log_when_clock_is_deleted(home):
    b = _gated_broker()
    _three(b.audit)
    before = b._clock_now()
    b.audit.clock_path.unlink()
    assert b._clock_now() == before


def test_expired_approval_stays_expired_when_clock_is_deleted(home):
    """Previously _clock_now() fell back to 0 with .clock gone, which
    resurrected every expired approval (exp >= 0)."""
    b = _gated_broker()
    b.approve("x", ttl=1)
    _three(b.audit)  # advance the clock past the approval's expiry
    b.audit.clock_path.unlink()
    with pytest.raises(ApprovalRequired):
        b.check_injectable("x")


def test_live_approval_stays_live_when_clock_is_deleted(home):
    b = _gated_broker()
    b.approve("x", ttl=100)
    b.audit.clock_path.unlink()
    assert b.check_injectable("x").sm_name == "dostal-x"
