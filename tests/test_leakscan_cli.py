"""`portunus leak-scan` / `portunus leak` CLI surface (portunus-leak-scan
Slice 3)."""
import ast
import inspect
import json

from portunus import AuditChain, Registry
from portunus.cli import (
    cmd_leak_mark_rotated,
    cmd_leak_scan,
    cmd_leak_scan_config_add_path,
    cmd_leak_scan_config_show,
    cmd_leak_status,
    main,
)
from portunus.leakscan import add_scan_path


def test_leak_scan_with_no_config_reports_explicitly(home, capsys):
    rc = main(["leak-scan"])
    out = capsys.readouterr().out
    assert rc == 0
    assert "no scan paths configured" in out


def test_leak_scan_config_add_show_remove_round_trips(home, capsys):
    rc = main(["leak-scan", "config", "add-path", "/tmp/*.log"])
    assert rc == 0
    capsys.readouterr()

    rc = main(["leak-scan", "config", "show", "--json"])
    out = capsys.readouterr().out
    assert rc == 0
    assert json.loads(out) == ["/tmp/*.log"]

    rc = main(["leak-scan", "config", "remove-path", "/tmp/*.log"])
    assert rc == 0
    capsys.readouterr()

    rc = main(["leak-scan", "config", "show", "--json"])
    out = capsys.readouterr().out
    assert json.loads(out) == []


def test_leak_scan_finds_and_reports_a_real_leak(home, tmp_path, capsys):
    Registry().add("x", "sm-x")
    import os

    os.environ["PORTUNUS_BACKEND"] = "mock"
    os.environ["PORTUNUS_MOCK_SM_X"] = "SECRET-VALUE-abc123-xyz"
    try:
        f = tmp_path / "app.log"
        f.write_text("uh oh: SECRET-VALUE-abc123-xyz\n")
        add_scan_path(str(f))

        rc = main(["leak-scan", "--json"])
        out = capsys.readouterr().out
        assert rc == 1  # new findings -> non-zero exit
        findings = json.loads(out)
        assert findings == [{"ref_name": "x", "path": str(f), "line_number": 1}]
    finally:
        del os.environ["PORTUNUS_BACKEND"]
        del os.environ["PORTUNUS_MOCK_SM_X"]


def test_leak_scan_rerun_with_no_new_findings_exits_zero(home, tmp_path, capsys):
    Registry().add("x", "sm-x")
    import os

    os.environ["PORTUNUS_BACKEND"] = "mock"
    os.environ["PORTUNUS_MOCK_SM_X"] = "SECRET-VALUE-abc123-xyz"
    try:
        f = tmp_path / "app.log"
        f.write_text("uh oh: SECRET-VALUE-abc123-xyz\n")
        add_scan_path(str(f))

        main(["leak-scan"])
        capsys.readouterr()
        rc = main(["leak-scan"])
        out = capsys.readouterr().out
        assert rc == 0
        assert "no new findings" in out
    finally:
        del os.environ["PORTUNUS_BACKEND"]
        del os.environ["PORTUNUS_MOCK_SM_X"]


def test_leak_status_and_mark_rotated_round_trip(home, tmp_path, capsys):
    Registry().add("x", "sm-x")
    import os

    os.environ["PORTUNUS_BACKEND"] = "mock"
    os.environ["PORTUNUS_MOCK_SM_X"] = "SECRET-VALUE-abc123-xyz"
    try:
        f = tmp_path / "app.log"
        f.write_text("uh oh: SECRET-VALUE-abc123-xyz\n")
        add_scan_path(str(f))
        main(["leak-scan"])
        capsys.readouterr()

        rc = main(["leak", "status", "x", "--json"])
        out = capsys.readouterr().out
        summary = json.loads(out)
        assert rc == 0
        assert summary["severity"] == "warn"
        assert summary["finding_count"] == 1

        rc = main(["leak", "mark-rotated", "x"])
        capsys.readouterr()
        assert rc == 0

        rc = main(["leak", "status", "x", "--json"])
        out = capsys.readouterr().out
        summary = json.loads(out)
        assert summary["severity"] is None
    finally:
        del os.environ["PORTUNUS_BACKEND"]
        del os.environ["PORTUNUS_MOCK_SM_X"]


def test_leak_status_with_no_findings_at_all_says_so(home, capsys):
    rc = main(["leak", "status"])
    out = capsys.readouterr().out
    assert rc == 0
    assert "no references with active leak findings" in out


def test_leak_scan_run_is_audited(home, tmp_path, capsys):
    Registry().add("x", "sm-x")
    import os

    os.environ["PORTUNUS_BACKEND"] = "mock"
    os.environ["PORTUNUS_MOCK_SM_X"] = "SECRET-VALUE-abc123-xyz"
    try:
        f = tmp_path / "app.log"
        f.write_text("uh oh: SECRET-VALUE-abc123-xyz\n")
        add_scan_path(str(f))
        main(["leak-scan"])
        capsys.readouterr()

        entries = AuditChain().entries()
        actions = [e["action"] for e in entries]
        assert "leak-scan" in actions
        assert "leak-scan-finding" in actions
        # never a value in any audit entry
        for e in entries:
            assert "SECRET-VALUE-abc123-xyz" not in json.dumps(e)
    finally:
        del os.environ["PORTUNUS_BACKEND"]
        del os.environ["PORTUNUS_MOCK_SM_X"]


def test_build_parser_still_parses_cleanly_after_leak_commands(home):
    from portunus.cli import build_parser

    parser = build_parser()
    args = parser.parse_args(["leak-scan"])
    assert args.func.__name__ == "cmd_leak_scan"
    args = parser.parse_args(["leak", "status"])
    assert args.func.__name__ == "cmd_leak_status"


def test_leak_cli_handlers_never_reference_a_value_source_check():
    """AST-level structural check, matching every prior CLI story this
    session -- these handlers only ever print ref_name/path/line_number/
    severity, never a value."""
    for fn in (
        cmd_leak_scan,
        cmd_leak_scan_config_add_path,
        cmd_leak_scan_config_show,
        cmd_leak_status,
        cmd_leak_mark_rotated,
    ):
        src = inspect.getsource(fn)
        tree = ast.parse(src)
        code = ast.unparse(tree)
        assert ".access(" not in code
