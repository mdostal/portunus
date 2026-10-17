"""`portunus prompt` / `portunus_prompt`: the human types a secret into an
out-of-band surface and the caller only ever gets metadata back (PANT-1119,
GH #210). Every test that stores a value also checks the value never shows
up in stdout/stderr, the registry, or the audit log."""
import json
import os
import subprocess
import threading
import time
from types import SimpleNamespace

import pytest

from portunus import prompt as prompt_mod
from portunus.cli import main
from portunus.localvault import LocalEncryptedBackend

SECRET = " s3kr3t pass-0xCAFE "  # leading/trailing spaces are deliberate


@pytest.fixture(autouse=True)
def _local_backend(monkeypatch):
    monkeypatch.delenv("PORTUNUS_BACKEND", raising=False)


class FakeRunner:
    """Stands in for subprocess.run: answers each dialog call in turn."""

    def __init__(self, *answers):
        self.answers = list(answers)
        self.calls = []

    def __call__(self, argv, **kwargs):
        self.calls.append((argv, kwargs))
        rc, out = self.answers.pop(0)
        return subprocess.CompletedProcess(argv, rc, stdout=out, stderr="")


def _use_runner(monkeypatch, runner, surfaces=("kdialog",)):
    monkeypatch.setattr(prompt_mod.subprocess, "run", runner)
    monkeypatch.setattr(prompt_mod, "available_surfaces", lambda **kw: list(surfaces))


def _assert_no_leak(home, captured):
    assert SECRET.strip() not in captured.out
    assert SECRET.strip() not in captured.err
    for path in home.rglob("*"):
        if path.is_file() and path.name not in ("vault", "master.key"):
            assert SECRET.strip().encode() not in path.read_bytes(), path


# --- surface detection ------------------------------------------------------

def test_no_gui_session_means_no_surfaces():
    which = lambda b: f"/usr/bin/{b}"
    assert prompt_mod.available_surfaces(env={}, platform="linux", which=which) == []


def test_linux_surface_order():
    present = {"zenity", "kdialog", "xterm", "pinentry-qt"}
    which = lambda b: f"/usr/bin/{b}" if b in present else None
    got = prompt_mod.available_surfaces(env={"DISPLAY": ":0"}, platform="linux", which=which)
    assert got == ["kdialog", "zenity", "pinentry", "xterm"]


def test_curses_pinentry_is_not_a_surface():
    which = lambda b: "/usr/bin/pinentry-curses" if b == "pinentry-curses" else None
    assert prompt_mod.available_surfaces(env={"WAYLAND_DISPLAY": "w"}, platform="linux", which=which) == []


def test_macos_surfaces():
    which = lambda b: "/usr/bin/osascript" if b == "osascript" else None
    assert prompt_mod.available_surfaces(env={}, platform="darwin", which=which) == [
        "osascript", "terminal-app",
    ]


def test_osascript_dialog_escapes_quotes():
    argv = prompt_mod._dialog_argv("osascript", 'T"', 'say "hi" \\ ok')
    assert 'with hidden answer' in argv[2]
    assert '"say \\"hi\\" \\\\ ok"' in argv[2]


# --- CLI: dialog surfaces ---------------------------------------------------

def test_prompt_stores_value_and_returns_metadata_only(home, monkeypatch, capsys):
    runner = FakeRunner((0, SECRET + "\n"))
    _use_runner(monkeypatch, runner)

    rc = main(["prompt", "sudo-pw", "dostal-sudo", "--provider", "linux",
               "--description", "sudo on workstation"])
    captured = capsys.readouterr()
    assert rc == 0
    result = json.loads(captured.out)
    assert result == {"name": "sudo-pw", "state": "dropped", "stored": True,
                      "gated": False, "surface": "kdialog"}
    # Whitespace kept: only the dialog's own newline is removed.
    assert LocalEncryptedBackend().access("dostal-sudo") == SECRET
    # The human-facing text names the secret; the argv never carries a value.
    argv = runner.calls[0][0]
    assert argv[:4] == ["kdialog", "--title", "Portunus: enter a secret", "--password"]
    assert "sudo-pw" in argv[4] and "sudo on workstation" in argv[4]
    _assert_no_leak(home, captured)


def test_prompt_enable_and_gate(home, monkeypatch, capsys):
    _use_runner(monkeypatch, FakeRunner((0, SECRET + "\n")))
    rc = main(["prompt", "sudo-pw", "dostal-sudo", "--enable", "--gate"])
    result = json.loads(capsys.readouterr().out)
    assert rc == 0
    assert result["state"] == "enabled"
    assert result["gated"] is True


def test_prompt_cancel(home, monkeypatch, capsys):
    _use_runner(monkeypatch, FakeRunner((1, "")))
    rc = main(["prompt", "sudo-pw", "dostal-sudo"])
    assert rc == 2
    assert json.loads(capsys.readouterr().out) == {"cancelled": True}
    main(["reg", "json"])
    assert "sudo-pw" not in capsys.readouterr().out


def test_prompt_timeout_reports_cancelled(home, monkeypatch, capsys):
    def runner(argv, **kwargs):
        raise subprocess.TimeoutExpired(argv, kwargs["timeout"])
    _use_runner(monkeypatch, runner)
    rc = main(["prompt", "sudo-pw", "dostal-sudo", "--timeout", "1"])
    assert rc == 2
    assert json.loads(capsys.readouterr().out) == {"cancelled": True, "reason": "timeout"}


def test_prompt_confirm_mismatch_stores_nothing(home, monkeypatch, capsys):
    _use_runner(monkeypatch, FakeRunner((0, SECRET + "\n"), (0, "other\n")))
    rc = main(["prompt", "sudo-pw", "dostal-sudo", "--confirm"])
    captured = capsys.readouterr()
    assert rc == 1
    assert json.loads(captured.out) == {"error": "values did not match, nothing stored"}
    _assert_no_leak(home, captured)


def test_prompt_confirm_match(home, monkeypatch, capsys):
    runner = FakeRunner((0, SECRET + "\n"), (0, SECRET + "\n"))
    _use_runner(monkeypatch, runner)
    assert main(["prompt", "sudo-pw", "dostal-sudo", "--confirm"]) == 0
    assert len(runner.calls) == 2


def test_prompt_empty_value_is_an_error(home, monkeypatch, capsys):
    _use_runner(monkeypatch, FakeRunner((0, "\n")))
    assert main(["prompt", "sudo-pw", "dostal-sudo"]) == 1
    assert "empty" in json.loads(capsys.readouterr().out)["error"]


def test_prompt_without_any_surface_fails_cleanly(home, monkeypatch, capsys):
    monkeypatch.setattr(prompt_mod, "available_surfaces", lambda **kw: [])
    rc = main(["prompt", "sudo-pw", "dostal-sudo"])
    assert rc == 1
    assert "no human-facing input surface" in json.loads(capsys.readouterr().out)["error"]


def test_prompt_refuses_non_local_backend(home, monkeypatch, capsys):
    monkeypatch.setenv("PORTUNUS_BACKEND", "mock")
    rc = main(["prompt", "sudo-pw", "dostal-sudo"])
    assert rc == 1
    assert "local-encrypted backend" in capsys.readouterr().out


def test_pinentry_protocol_decodes_value():
    def runner(argv, input, **kwargs):
        assert "GETPIN" in input
        return subprocess.CompletedProcess(argv, 0, stdout="OK\nOK\nD a%25b%0Ac \nOK\n", stderr="")
    which = lambda b: "/usr/bin/pinentry-qt" if b == "pinentry-qt" else None
    req = prompt_mod.PromptRequest(name="n", sm_name="s")
    assert prompt_mod.read_from_dialog("pinentry", req, runner=runner, which=which) == "a%b\nc "


def test_pinentry_cancel():
    def runner(argv, input, **kwargs):
        return subprocess.CompletedProcess(argv, 0, stdout="OK\nERR 83886179 Operation cancelled\n", stderr="")
    which = lambda b: "/usr/bin/pinentry-qt" if b == "pinentry-qt" else None
    req = prompt_mod.PromptRequest(name="n", sm_name="s")
    with pytest.raises(prompt_mod.PromptCancelled):
        prompt_mod.read_from_dialog("pinentry", req, runner=runner, which=which)


# --- terminal surface (real FIFO) ------------------------------------------

class FakeTerminal:
    """Stands in for Popen of a terminal window: writes `message` down the
    FIFO the way `portunus _tty-entry` would, then 'closes the window'."""

    def __init__(self, message):
        self.message = message
        self.argv = None

    def __call__(self, argv, **kwargs):
        self.argv = argv
        fifo = argv[argv.index("_tty-entry") + 1]
        done = threading.Event()

        def window():
            time.sleep(0.3)
            if self.message is not None:
                with open(fifo, "w") as fh:
                    fh.write(json.dumps(self.message) + "\n")
            done.set()

        threading.Thread(target=window, daemon=True).start()
        return SimpleNamespace(poll=lambda: 0 if done.is_set() else None, terminate=lambda: None)


def test_terminal_surface_reads_value_over_fifo(home, monkeypatch, capsys):
    term = FakeTerminal({"value": SECRET})
    monkeypatch.setattr(prompt_mod.subprocess, "Popen", term)
    rc = main(["prompt", "sudo-pw", "dostal-sudo", "--surface", "konsole"])
    captured = capsys.readouterr()
    assert rc == 0
    assert json.loads(captured.out)["surface"] == "konsole"
    assert term.argv[:3] == ["konsole", "--nofork", "-e"]
    assert SECRET not in " ".join(term.argv)
    assert LocalEncryptedBackend().access("dostal-sudo") == SECRET
    _assert_no_leak(home, captured)


def test_terminal_window_closed_without_answer_is_cancelled(home, monkeypatch, capsys):
    monkeypatch.setattr(prompt_mod.subprocess, "Popen", FakeTerminal(None))
    rc = main(["prompt", "sudo-pw", "dostal-sudo", "--surface", "xterm"])
    assert rc == 2
    assert json.loads(capsys.readouterr().out) == {"cancelled": True}


def test_terminal_cleans_up_fifo_dir(home, monkeypatch, capsys):
    term = FakeTerminal({"value": SECRET})
    monkeypatch.setattr(prompt_mod.subprocess, "Popen", term)
    main(["prompt", "sudo-pw", "dostal-sudo", "--surface", "xterm"])
    fifo = term.argv[term.argv.index("_tty-entry") + 1]
    assert not os.path.exists(os.path.dirname(fifo))


def test_tty_entry_writes_value_line(tmp_path, monkeypatch, capsys):
    answers = iter([SECRET, SECRET])
    monkeypatch.setattr("getpass.getpass", lambda *a, **k: next(answers))
    out = tmp_path / "answer"
    assert main(["_tty-entry", str(out), "--banner", "hello"]) == 0
    assert json.loads(out.read_text()) == {"value": SECRET}
    assert SECRET.strip() not in capsys.readouterr().out


def test_tty_entry_mismatch(tmp_path, monkeypatch, capsys):
    answers = iter([SECRET, "nope"])
    monkeypatch.setattr("getpass.getpass", lambda *a, **k: next(answers))
    out = tmp_path / "answer"
    main(["_tty-entry", str(out)])
    assert "error" in json.loads(out.read_text())


# --- ui surface -------------------------------------------------------------

def test_ui_surface_creates_request_and_opens_deep_link(home, monkeypatch, capsys):
    opened = []
    monkeypatch.setattr("portunus.cli._open_ui", lambda name: opened.append(name) or {"url": f"u?fulfill={name}"})
    rc = main(["prompt", "sudo-pw", "dostal-sudo", "--surface", "ui"])
    result = json.loads(capsys.readouterr().out)
    assert rc == 0
    assert opened == ["sudo-pw"]
    assert result == {"name": "sudo-pw", "state": "requested", "stored": False,
                      "pending": True, "surface": "ui", "url": "u?fulfill=sudo-pw"}


# --- MCP --------------------------------------------------------------------

def test_mcp_prompt_tool(home, monkeypatch):
    from portunus import mcp_server

    _use_runner(monkeypatch, FakeRunner((0, SECRET + "\n")))
    result = mcp_server.portunus_prompt(name="sudo-pw", sm_name="dostal-sudo", enable=True)
    assert result == {"name": "sudo-pw", "state": "enabled", "stored": True,
                      "gated": False, "surface": "kdialog"}
    assert SECRET.strip() not in json.dumps(result)
    tool_names = {t.name for t in mcp_server.mcp._tool_manager.list_tools()}
    assert "portunus_prompt" in tool_names


def test_mcp_prompt_rejects_unknown_surface(home):
    from portunus import mcp_server

    assert "error" in mcp_server.portunus_prompt(name="a", sm_name="b", surface="carrier-pigeon")
