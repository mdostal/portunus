"""End-to-end: drop -> enable -> inject, entirely through the CLI, on the
default (local-encrypted) backend. Proves the acceptance criterion from
DOS-726 directly: zero plaintext in stdout/stderr, the vault file, or the
audit log at any point in the lifecycle."""
import io
import os

import pytest

from portunus.audit import AuditChain
from portunus.cli import main

SECRET = "s3kr3t-do-not-leak-0xCAFE"


@pytest.fixture(autouse=True)
def _local_backend(monkeypatch):
    # The CLI defaults to the local backend already; be explicit and make
    # sure no stray env from another test selects mock/gcloud.
    monkeypatch.delenv("PORTUNUS_BACKEND", raising=False)


def test_drop_lands_dropped_and_is_not_injectable(home, capsys):
    value_file = home / "value.txt"
    value_file.write_text(SECRET + "\n")

    rc = main(["drop", "shared-test", "dostal-shared-test", "--value-file", str(value_file)])
    assert rc == 0
    out = capsys.readouterr().out
    assert SECRET not in out
    assert "dropped" in out

    # dropped state fails closed: resolve must refuse.
    rc = main(["resolve", "{{secret:shared-test}}"])
    captured = capsys.readouterr()
    assert rc == 1
    assert SECRET not in captured.out
    assert SECRET not in captured.err
    assert "dropped" in captured.err


def test_drop_enable_inject_lifecycle(home, capsys):
    value_file = home / "value.txt"
    value_file.write_text(SECRET + "\n")
    main(["drop", "shared-test", "dostal-shared-test", "--value-file", str(value_file)])
    capsys.readouterr()

    rc = main(["state", "shared-test", "enabled"])
    assert rc == 0
    capsys.readouterr()

    rc = main(["resolve", "key={{secret:shared-test}}"])
    assert rc == 0
    path = capsys.readouterr().out.strip()
    try:
        assert open(path).read() == f"key={SECRET}"
    finally:
        os.unlink(path)


def test_drop_never_writes_plaintext_to_disk_or_audit(home):
    value_file = home / "value.txt"
    value_file.write_text(SECRET + "\n")
    main(["drop", "shared-test", "dostal-shared-test", "--value-file", str(value_file)])

    for name in ("vault.enc.json", "registry.json", "audit.log"):
        path = home / name
        if path.exists():
            assert SECRET not in path.read_text()

    audit = AuditChain()
    drops = [e for e in audit.entries() if e["action"] == "drop"]
    assert len(drops) == 1
    assert drops[0]["secret"] == "dostal-shared-test"


def test_drop_neither_stdin_nor_value_file_prompts_interactively(home, monkeypatch, capsys):
    """portunus-secure-entry Story 02: neither flag given no longer raises
    SystemExit at parse time -- it falls into the getpass-based interactive
    mode instead (see the dedicated interactive-mode tests below)."""
    calls = iter([SECRET, SECRET])
    monkeypatch.setattr("getpass.getpass", lambda *a, **k: next(calls))
    rc = main(["drop", "shared-test", "dostal-shared-test"])
    assert rc == 0
    out = capsys.readouterr().out
    assert SECRET not in out


def test_drop_accepts_provider_project_env_tags(home, capsys):
    """story 06 prep: the UI's add-secret form needs the full tag schema
    from story 01 available on drop, not just legacy scope/kind."""
    value_file = home / "value.txt"
    value_file.write_text(SECRET + "\n")

    rc = main([
        "drop", "vercel-mdostal", "sm-vercel-mdostal", "--value-file", str(value_file),
        "--provider", "vercel", "--project", "mdostal.com", "--env", "prod",
        "--tags", "team=platform",
    ])
    assert rc == 0

    from portunus import Registry
    ref = Registry().require("vercel-mdostal")
    assert ref.provider == "vercel"
    assert ref.project == "mdostal.com"
    assert ref.env == "prod"
    assert ref.tags == {"team": "platform"}
    assert ref.state == "dropped"


def test_drop_accepts_backend_flag(home, capsys):
    """story 01 (portunus-metadata-and-rotation-provenance): cmd_drop gained
    --backend, matching Reference.backend / registry.add's existing kwarg --
    portunus_drop (MCP) and drop-bulk's per-entry backend already supported
    this; the CLI's one-off drop command didn't."""
    value_file = home / "value.txt"
    value_file.write_text(SECRET + "\n")

    rc = main([
        "drop", "local-only-secret", "sm-local-only", "--value-file", str(value_file),
        "--backend", "local",
    ])
    assert rc == 0

    from portunus import Registry
    ref = Registry().require("local-only-secret")
    assert ref.backend == "local"


def test_drop_backend_defaults_empty(home, capsys):
    value_file = home / "value.txt"
    value_file.write_text(SECRET + "\n")

    rc = main(["drop", "shared-test", "dostal-shared-test", "--value-file", str(value_file)])
    assert rc == 0

    from portunus import Registry
    ref = Registry().require("shared-test")
    assert ref.backend == ""


def test_drop_stdin_trims_leading_and_trailing_whitespace(home, monkeypatch, capsys):
    monkeypatch.setattr("sys.stdin", io.StringIO(f"  {SECRET}  \n"))
    rc = main(["drop", "shared-test", "dostal-shared-test", "--stdin"])
    assert rc == 0
    capsys.readouterr()

    main(["state", "shared-test", "enabled"])
    capsys.readouterr()
    rc = main(["resolve", f"key={{{{secret:shared-test}}}}"])
    assert rc == 0
    path = capsys.readouterr().out.strip()
    try:
        assert open(path).read() == f"key={SECRET}"
    finally:
        os.unlink(path)


def test_drop_stdin_trims_a_trailing_carriage_return(home, monkeypatch, capsys):
    """A classic artifact of copy-pasting a token from a Windows-authored
    file: readline() includes the \\r before its trailing \\n."""
    monkeypatch.setattr("sys.stdin", io.StringIO(f"{SECRET}\r\n"))
    rc = main(["drop", "shared-test", "dostal-shared-test", "--stdin"])
    assert rc == 0
    capsys.readouterr()

    main(["state", "shared-test", "enabled"])
    capsys.readouterr()
    main(["resolve", f"key={{{{secret:shared-test}}}}"])
    path = capsys.readouterr().out.strip()
    try:
        assert open(path).read() == f"key={SECRET}"
    finally:
        os.unlink(path)


def test_drop_value_file_trims_leading_and_trailing_whitespace(home, capsys):
    value_file = home / "value.txt"
    value_file.write_text(f"\n\n  {SECRET}  \n\n")

    rc = main(["drop", "shared-test", "dostal-shared-test", "--value-file", str(value_file)])
    assert rc == 0
    capsys.readouterr()

    main(["state", "shared-test", "enabled"])
    capsys.readouterr()
    main(["resolve", f"key={{{{secret:shared-test}}}}"])
    path = capsys.readouterr().out.strip()
    try:
        assert open(path).read() == f"key={SECRET}"
    finally:
        os.unlink(path)


def test_drop_stdin_all_whitespace_is_treated_as_empty(home, monkeypatch, capsys):
    monkeypatch.setattr("sys.stdin", io.StringIO("   \t  \n"))
    rc = main(["drop", "shared-test", "dostal-shared-test", "--stdin"])
    assert rc == 1
    err = capsys.readouterr().err
    assert "empty secret value" in err

    from portunus import Registry
    assert "shared-test" not in Registry()


def test_drop_stdin_preserves_internal_whitespace(home, monkeypatch, capsys):
    inner = "SECRET WITH SPACE-0xCAFE"
    monkeypatch.setattr("sys.stdin", io.StringIO(f"  {inner}  \n"))
    rc = main(["drop", "shared-test", "dostal-shared-test", "--stdin"])
    assert rc == 0
    capsys.readouterr()

    main(["state", "shared-test", "enabled"])
    capsys.readouterr()
    main(["resolve", f"key={{{{secret:shared-test}}}}"])
    path = capsys.readouterr().out.strip()
    try:
        assert open(path).read() == f"key={inner}"
    finally:
        os.unlink(path)


# --- portunus-secure-entry Story 02: interactive masked prompt ---------------

def test_drop_interactive_prompts_twice_and_stores_on_match(home, monkeypatch, capsys):
    calls = iter([f"  {SECRET}  ", f"  {SECRET}  "])
    monkeypatch.setattr("getpass.getpass", lambda *a, **k: next(calls))
    rc = main(["drop", "shared-test", "dostal-shared-test"])
    assert rc == 0
    capsys.readouterr()

    main(["state", "shared-test", "enabled"])
    capsys.readouterr()
    main(["resolve", f"key={{{{secret:shared-test}}}}"])
    path = capsys.readouterr().out.strip()
    try:
        assert open(path).read() == f"key={SECRET}"  # trimmed, same as Story 01
    finally:
        os.unlink(path)


def test_drop_interactive_mismatch_refuses_and_stores_nothing(home, monkeypatch, capsys):
    calls = iter([SECRET, SECRET + "-typo"])
    monkeypatch.setattr("getpass.getpass", lambda *a, **k: next(calls))
    rc = main(["drop", "shared-test", "dostal-shared-test"])
    assert rc == 1
    err = capsys.readouterr().err
    assert "did not match" in err
    assert SECRET not in err

    from portunus import Registry
    assert "shared-test" not in Registry()


def test_drop_stdin_never_triggers_getpass(home, monkeypatch, capsys):
    monkeypatch.setattr("sys.stdin", io.StringIO(f"{SECRET}\n"))
    called = []
    monkeypatch.setattr("getpass.getpass", lambda *a, **k: called.append(1) or SECRET)
    rc = main(["drop", "shared-test", "dostal-shared-test", "--stdin"])
    assert rc == 0
    assert called == []


def test_drop_value_file_never_triggers_getpass(home, monkeypatch, capsys):
    value_file = home / "value.txt"
    value_file.write_text(SECRET + "\n")
    called = []
    monkeypatch.setattr("getpass.getpass", lambda *a, **k: called.append(1) or SECRET)
    rc = main(["drop", "shared-test", "dostal-shared-test", "--value-file", str(value_file)])
    assert rc == 0
    assert called == []


def test_drop_interactive_empty_after_trim_is_refused(home, monkeypatch, capsys):
    calls = iter(["   ", "   "])
    monkeypatch.setattr("getpass.getpass", lambda *a, **k: next(calls))
    rc = main(["drop", "shared-test", "dostal-shared-test"])
    assert rc == 1
    assert "empty secret value" in capsys.readouterr().err

    from portunus import Registry
    assert "shared-test" not in Registry()


def test_drop_interactive_eof_is_caught_cleanly_not_a_raw_traceback(home, monkeypatch, capsys):
    """Verified directly: getpass() against closed/empty stdin (exactly what
    an agent's own non-interactive tool call provides) raises EOFError
    immediately -- this must become a clean CLI error, never an uncaught
    traceback."""
    def _raise(*a, **k):
        raise EOFError()

    monkeypatch.setattr("getpass.getpass", _raise)
    rc = main(["drop", "shared-test", "dostal-shared-test"])
    assert rc == 1
    err = capsys.readouterr().err
    assert "portunus:" in err  # the normal _err()-formatted message, not a traceback

    from portunus import Registry
    assert "shared-test" not in Registry()
