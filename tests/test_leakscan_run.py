"""scan-path config store, watermark persistence, and run_scan() orchestration
(portunus-leak-scan Slice 3, engine-level -- CLI-level tests live in
tests/test_leakscan_cli.py)."""
from portunus.leakscan import (
    add_scan_path,
    load_scan_paths,
    load_watermarks,
    remove_scan_path,
    run_scan,
)


def test_add_scan_path_persists(home):
    add_scan_path("/var/log/*.log")
    assert load_scan_paths() == ["/var/log/*.log"]


def test_add_scan_path_is_idempotent(home):
    add_scan_path("/var/log/*.log")
    add_scan_path("/var/log/*.log")
    assert load_scan_paths() == ["/var/log/*.log"]


def test_remove_scan_path(home):
    add_scan_path("/var/log/*.log")
    add_scan_path("/tmp/*.txt")
    remove_scan_path("/var/log/*.log")
    assert load_scan_paths() == ["/tmp/*.txt"]


def test_remove_unconfigured_path_is_a_harmless_no_op(home):
    add_scan_path("/var/log/*.log")
    remove_scan_path("/does/not/exist/*.log")
    assert load_scan_paths() == ["/var/log/*.log"]


def test_run_scan_with_no_configured_paths_reports_that_explicitly(stack):
    result = run_scan(stack["registry"], stack["broker"], stack["backend"])
    assert result.configured_paths == []
    assert result.findings == []


def test_run_scan_finds_a_real_leak_and_persists_findings(stack, tmp_path):
    stack["registry"].add("x", "sm-x")
    stack["backend"].set("sm-x", "SECRET-VALUE-abc123-xyz")
    f = tmp_path / "log.txt"
    f.write_text("uh oh: SECRET-VALUE-abc123-xyz\n")
    add_scan_path(str(f))

    result = run_scan(stack["registry"], stack["broker"], stack["backend"])
    assert len(result.findings) == 1
    assert result.findings[0].ref_name == "x"

    from portunus.leakscan import load_leak_status

    assert "x" in load_leak_status()


def test_run_scan_second_run_does_not_reduplicate_watermarks(stack, tmp_path):
    stack["registry"].add("x", "sm-x")
    stack["backend"].set("sm-x", "SECRET-VALUE-abc123-xyz")
    f = tmp_path / "log.txt"
    f.write_text("uh oh: SECRET-VALUE-abc123-xyz\n")
    add_scan_path(str(f))

    run_scan(stack["registry"], stack["broker"], stack["backend"])
    result2 = run_scan(stack["registry"], stack["broker"], stack["backend"])
    assert result2.findings == []
    assert str(f) in load_watermarks()
