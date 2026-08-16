"""leak-status store, severity derivation, mark-rotated (portunus-leak-scan
Slice 2). Locked from day one, matching views.py/roles.py -- and advisory
only: check_injectable()/resolve() must behave byte-identically whether or
not a reference has active leak findings, at any severity."""
import os
import subprocess
import sys

from portunus import Registry
from portunus.leakscan import (
    Finding,
    LeakStatus,
    load_leak_status,
    mark_rotated,
    record_findings,
    severity,
    summarize,
)

DAY = 86400.0


def test_new_finding_creates_status_with_matching_first_and_last(home):
    record_findings([Finding("x", "/var/log/a.log", 3, 0)], now=1000.0)
    statuses = load_leak_status()
    assert statuses["x"].findings[0].first_detected_at == 1000.0
    assert statuses["x"].findings[0].last_detected_at == 1000.0


def test_redetection_updates_last_detected_not_duplicated(home):
    record_findings([Finding("x", "/var/log/a.log", 3, 0)], now=1000.0)
    record_findings([Finding("x", "/var/log/a.log", 3, 0)], now=2000.0)
    statuses = load_leak_status()
    findings = statuses["x"].findings
    assert len(findings) == 1
    assert findings[0].first_detected_at == 1000.0
    assert findings[0].last_detected_at == 2000.0


def test_different_line_is_a_separate_finding(home):
    record_findings([Finding("x", "/var/log/a.log", 3, 0)], now=1000.0)
    record_findings([Finding("x", "/var/log/a.log", 9, 0)], now=1000.0)
    statuses = load_leak_status()
    assert len(statuses["x"].findings) == 2


def test_severity_warn_within_two_days(home):
    record_findings([Finding("x", "/var/log/a.log", 3, 0)], now=0.0)
    statuses = load_leak_status()
    assert severity(statuses["x"], now=2 * DAY) == "warn"


def test_severity_urgent_between_three_and_six_days(home):
    record_findings([Finding("x", "/var/log/a.log", 3, 0)], now=0.0)
    statuses = load_leak_status()
    assert severity(statuses["x"], now=3 * DAY) == "urgent"
    assert severity(statuses["x"], now=6.9 * DAY) == "urgent"


def test_severity_critical_at_seven_days_and_beyond(home):
    record_findings([Finding("x", "/var/log/a.log", 3, 0)], now=0.0)
    statuses = load_leak_status()
    assert severity(statuses["x"], now=7 * DAY) == "critical"
    assert severity(statuses["x"], now=30 * DAY) == "critical"


def test_severity_uses_earliest_first_detected_across_findings(home):
    record_findings([Finding("x", "/var/log/a.log", 3, 0)], now=0.0)
    record_findings([Finding("x", "/var/log/b.log", 1, 0)], now=6 * DAY)
    statuses = load_leak_status()
    # earliest finding is 7 days old at now=7*DAY -> critical, not warn
    assert severity(statuses["x"], now=7 * DAY) == "critical"


def test_no_findings_has_no_severity(home):
    status = LeakStatus(ref_name="x")
    assert severity(status, now=1000.0) is None


def test_mark_rotated_clears_active_findings(home):
    record_findings([Finding("x", "/var/log/a.log", 3, 0)], now=0.0)
    mark_rotated("x", now=10.0)
    statuses = load_leak_status()
    assert statuses["x"].findings == []


def test_mark_rotated_then_new_finding_resets_escalation_clock(home):
    record_findings([Finding("x", "/var/log/a.log", 3, 0)], now=0.0)
    mark_rotated("x", now=10 * DAY)
    record_findings([Finding("x", "/var/log/a.log", 3, 0)], now=10 * DAY)
    statuses = load_leak_status()
    assert severity(statuses["x"], now=10 * DAY) == "warn"


def test_mark_rotated_on_reference_with_no_findings_is_a_harmless_no_op(home):
    mark_rotated("never-leaked", now=10.0)
    statuses = load_leak_status()
    assert "never-leaked" not in statuses or statuses["never-leaked"].findings == []


def test_check_injectable_and_resolve_are_byte_identical_with_or_without_leak_findings(stack):
    """The defining advisory-only proof, mirroring
    test_check_injectable_and_retag_are_byte_identical_with_or_without_
    roles_configured (roles.py). Not "defaults to permissive" -- provably
    inert."""
    stack["registry"].add("x", "sm-x")
    stack["backend"].set("sm-x", "a-real-secret-value-123")

    before_ref = stack["broker"].check_injectable("x")
    before_value = stack["resolver"]._fetch("x")

    record_findings([Finding("x", "/var/log/a.log", 3, 0)], now=0.0)
    assert severity(load_leak_status()["x"], now=30 * DAY) == "critical"

    after_ref = stack["broker"].check_injectable("x")
    after_value = stack["resolver"]._fetch("x")

    assert before_ref == after_ref
    assert before_value == after_value == "a-real-secret-value-123"


def test_concurrent_record_findings_from_separate_processes_never_loses_a_write(home):
    """Real multi-process proof, matching views.py's own established
    technique (test_views.py) -- not a thread approximation."""
    barrier = home / "start-barrier"
    n = 10
    script = (
        "import time\n"
        "from pathlib import Path\n"
        "from portunus.leakscan import Finding, record_findings\n"
        f"barrier = Path({str(barrier)!r})\n"
        "deadline = time.monotonic() + 5\n"
        "while not barrier.exists() and time.monotonic() < deadline:\n"
        "    pass\n"
        "import sys\n"
        "i = sys.argv[1]\n"
        "record_findings([Finding(f'ref-{i}', '/var/log/a.log', 1, 0)], now=1000.0)\n"
    )
    procs = [
        subprocess.Popen([sys.executable, "-c", script, str(i)], env=os.environ.copy())
        for i in range(n)
    ]
    barrier.write_text("go")
    for p in procs:
        assert p.wait(timeout=10) == 0

    statuses = load_leak_status()
    assert sorted(statuses.keys()) == sorted(f"ref-{i}" for i in range(n))


def test_leakstatus_store_never_persists_a_value(home):
    record_findings([Finding("x", "/var/log/a.log", 3, 0)], now=1000.0)
    raw = (home / "leak-status.json").read_text()
    assert "a-real-secret-value" not in raw


# ---------------------------------------------------------------------------
# summarize()'s detail param (portunus-leak-visibility Story 01)
# ---------------------------------------------------------------------------


def test_summarize_without_detail_is_byte_identical_to_before(home):
    """No accidental behavior change for existing callers -- detail=False
    (the default) must preserve the exact prior shape."""
    record_findings([Finding("x", "/var/log/a.log", 3, 0)], now=1000.0)
    status = load_leak_status()["x"]
    assert summarize(status, now=2000.0) == {
        "ref_name": "x",
        "severity": "warn",
        "finding_count": 1,
        "first_detected_at": 1000.0,
        "last_detected_at": 1000.0,
    }


def test_summarize_detail_includes_findings_list_and_distinct_files(home):
    record_findings([Finding("x", "/var/log/a.log", 3, 0)], now=1000.0)
    record_findings([Finding("x", "/var/log/a.log", 9, 0)], now=1000.0)
    record_findings([Finding("x", "/var/log/b.log", 1, 0)], now=1000.0)
    status = load_leak_status()["x"]
    result = summarize(status, now=2000.0, detail=True)
    assert result["finding_count"] == 3
    assert result["distinct_files"] == 2
    assert len(result["findings"]) == 3
    assert {f["path"] for f in result["findings"]} == {"/var/log/a.log", "/var/log/b.log"}


def test_summarize_detail_distinct_files_not_raw_finding_count(home):
    """The headline number is distinct FILES, not raw findings -- a
    transcript can match the same secret on many lines without that
    meaning 'leaked in many conversations.'"""
    for line in range(1, 6):
        record_findings([Finding("x", "/var/log/same-file.log", line, 0)], now=1000.0)
    status = load_leak_status()["x"]
    result = summarize(status, now=2000.0, detail=True)
    assert result["finding_count"] == 5
    assert result["distinct_files"] == 1


def test_summarize_detail_no_findings_is_empty_not_an_error(home):
    status = LeakStatus(ref_name="never-leaked")
    result = summarize(status, now=1000.0, detail=True)
    assert result["findings"] == []
    assert result["distinct_files"] == 0
