"""The `t3` prompt surface: `portunus prompt` / `portunus_prompt` handing the
human's typing over to a T3 Code secret card.

Two calls. The first returns a handoff and stores nothing. The second takes
the 0600 file the card wrote, stores its content and deletes the file. No
test here lets the value reach stdout/stderr, the registry or the audit log.
"""
import json
import os
import subprocess
from types import SimpleNamespace

import pytest

from portunus import prompt as prompt_mod
from portunus.audit import AuditChain
from portunus.cli import main
from portunus.localvault import LocalEncryptedBackend
from portunus.registry import Registry

SECRET = " hunter2-test "  # leading/trailing spaces are deliberate
CARD_PATH = "<file path returned by the card>"


@pytest.fixture(autouse=True)
def _local_backend(monkeypatch):
    monkeypatch.delenv("PORTUNUS_BACKEND", raising=False)


@pytest.fixture
def card(tmp_path_factory):
    """Write a file the way a T3 Code card does: 0600, in a directory that
    is not the Portunus home."""
    directory = tmp_path_factory.mktemp("t3-card")

    def write(content=SECRET + "\n", mode=0o600, name="secret"):
        path = directory / name
        path.write_bytes(content if isinstance(content, bytes) else content.encode())
        path.chmod(mode)
        return path

    return write


class FakeRunner:
    def __init__(self, *answers):
        self.answers = list(answers)
        self.calls = []

    def __call__(self, argv, **kwargs):
        self.calls.append(argv)
        rc, out = self.answers.pop(0)
        return subprocess.CompletedProcess(argv, rc, stdout=out, stderr="")


def _use_dialog(monkeypatch, runner):
    monkeypatch.setattr(prompt_mod.subprocess, "run", runner)
    monkeypatch.setattr(prompt_mod, "available_surfaces", lambda **kw: ["kdialog"])


def _run(capsys, *argv):
    rc = main(["prompt", *argv])
    captured = capsys.readouterr()
    return rc, json.loads(captured.out), captured


def _assert_untouched(home):
    """Nothing was registered, stored or audited."""
    assert "sudo-pw" not in Registry()
    assert not (home / "vault.enc.json").exists()
    assert AuditChain().entries() == []


def _assert_no_leak(home, captured):
    needle = SECRET.strip()
    assert needle not in captured.out
    assert needle not in captured.err
    for path in home.rglob("*"):
        if path.is_file() and path.name not in ("vault", "master.key"):
            assert needle.encode() not in path.read_bytes(), path


# --- the surface itself -----------------------------------------------------

def test_t3_is_a_surface_but_not_a_local_dialog():
    assert "t3" in prompt_mod.SURFACES
    assert "t3" not in prompt_mod.DIALOG_SURFACES + prompt_mod.TERMINAL_SURFACES
    which = lambda b: f"/usr/bin/{b}"
    for platform in ("linux", "darwin"):
        found = prompt_mod.available_surfaces(
            env={"DISPLAY": ":0", "T3CODE_THREAD_ID": "thread-1"}, platform=platform, which=which,
        )
        assert "t3" not in found


# --- step one: the handoff --------------------------------------------------

def test_handoff_shape_and_nothing_is_stored(home, capsys):
    rc, result, _ = _run(
        capsys, "sudo-pw", "dostal-sudo", "--surface", "t3", "--provider", "linux",
        "--description", "sudo on workstation", "--enable", "--gate",
    )
    assert rc == 0
    assert result["stored"] is False
    assert result["pending"] is True
    assert result["surface"] == "t3"
    assert result["name"] == "sudo-pw"
    assert result["card"] == {
        "tool": "mcp__t3-code__request_secret",
        "arguments": {
            "label": "sudo on workstation",
            "reason": "The value is stored in your Portunus vault as 'sudo-pw' and the agent "
                      "never sees it. Each use will need your approval.",
            "delivery": "file",
        },
    }
    assert result["then"]["tool"] == "portunus_prompt"
    assert result["then"]["arguments"] == {
        "name": "sudo-pw", "sm_name": "dostal-sudo", "value_file": CARD_PATH,
        "provider": "linux", "description": "sudo on workstation",
        "enable": True, "gate": True,
    }
    assert result["then"]["cli"] == [
        "portunus", "prompt", "sudo-pw", "dostal-sudo", "--value-file", CARD_PATH,
        "--provider", "linux", "--description", "sudo on workstation", "--enable", "--gate",
    ]
    assert "Do not read" in result["note"]
    _assert_untouched(home)


def test_handoff_label_falls_back_to_the_name_and_stays_one_short_line():
    plain = prompt_mod.t3_handoff(prompt_mod.PromptRequest(name="sudo-pw", sm_name="s"))
    assert plain["card"]["arguments"]["label"] == "sudo-pw"
    assert "approval" not in plain["card"]["arguments"]["reason"]
    assert "cli" not in plain["then"]

    long = prompt_mod.t3_handoff(prompt_mod.PromptRequest(
        name="n", sm_name="s", description="line one\nline two " + "x" * 200,
    ))
    label = long["card"]["arguments"]["label"]
    assert "\n" not in label
    assert len(label) <= 80
    assert label.startswith("line one line two")


def test_handoff_carries_every_metadata_argument(home):
    req = prompt_mod.PromptRequest(
        name="n", sm_name="s", scope="shared", kind="api-key", org="acme", provider="stripe",
        project="shop", env="prod", tags={"team": "pay"}, description="d", purpose="p",
        group="shop/stripe", repo="acme/shop",
    )
    args = prompt_mod.t3_handoff(req)["then"]["arguments"]
    assert args == {
        "name": "n", "sm_name": "s", "value_file": CARD_PATH, "provider": "stripe",
        "project": "shop", "org": "acme", "env": "prod", "scope": "shared",
        "description": "d", "purpose": "p", "group": "shop/stripe", "repo": "acme/shop",
        "kind": "api-key", "tags": {"team": "pay"},
    }


def test_handoff_cli_keeps_an_explicit_home(tmp_path, capsys):
    other = tmp_path / "other-home"
    rc = main(["--home", str(other), "prompt", "sudo-pw", "dostal-sudo", "--surface", "t3"])
    result = json.loads(capsys.readouterr().out)
    assert rc == 0
    assert result["then"]["cli"][:3] == ["portunus", "--home", str(other)]


def test_handoff_refuses_non_local_backend(home, monkeypatch, capsys):
    monkeypatch.setenv("PORTUNUS_BACKEND", "mock")
    rc, result, _ = _run(capsys, "sudo-pw", "dostal-sudo", "--surface", "t3")
    assert rc == 1
    assert "local-encrypted backend" in result["error"]


# --- auto-selection ---------------------------------------------------------

def test_auto_picks_t3_inside_a_t3_thread(home, monkeypatch, capsys):
    monkeypatch.setenv("T3CODE_THREAD_ID", "thread-1")
    runner = FakeRunner()
    _use_dialog(monkeypatch, runner)
    rc, result, _ = _run(capsys, "sudo-pw", "dostal-sudo")
    assert rc == 0
    assert result["surface"] == "t3" and result["pending"] is True
    assert runner.calls == []  # no desktop dialog was opened
    _assert_untouched(home)


def test_auto_opens_a_desktop_dialog_outside_t3(home, monkeypatch, capsys):
    _use_dialog(monkeypatch, FakeRunner((0, SECRET + "\n")))
    rc, result, _ = _run(capsys, "sudo-pw", "dostal-sudo")
    assert rc == 0
    assert result["surface"] == "kdialog" and result["stored"] is True


def test_env_override_restores_the_desktop_dialog(home, monkeypatch, capsys):
    monkeypatch.setenv("T3CODE_THREAD_ID", "thread-1")
    monkeypatch.setenv("PORTUNUS_PROMPT_SURFACE", "kdialog")
    runner = FakeRunner((0, SECRET + "\n"))
    monkeypatch.setattr(prompt_mod.subprocess, "run", runner)
    rc, result, _ = _run(capsys, "sudo-pw", "dostal-sudo")
    assert rc == 0
    assert result["surface"] == "kdialog"
    assert runner.calls[0][0] == "kdialog"


def test_env_override_can_select_t3_outside_a_thread(home, monkeypatch, capsys):
    monkeypatch.setenv("PORTUNUS_PROMPT_SURFACE", "t3")
    rc, result, _ = _run(capsys, "sudo-pw", "dostal-sudo")
    assert rc == 0 and result["surface"] == "t3"


def test_env_override_with_an_unknown_surface_is_an_error(home, monkeypatch, capsys):
    monkeypatch.setenv("PORTUNUS_PROMPT_SURFACE", "carrier-pigeon")
    rc, result, _ = _run(capsys, "sudo-pw", "dostal-sudo")
    assert rc == 1
    assert "PORTUNUS_PROMPT_SURFACE" in result["error"]
    _assert_untouched(home)


def test_explicit_surface_wins_over_thread_and_override(home, monkeypatch, capsys):
    monkeypatch.setenv("T3CODE_THREAD_ID", "thread-1")
    monkeypatch.setenv("PORTUNUS_PROMPT_SURFACE", "t3")
    runner = FakeRunner((0, SECRET + "\n"))
    monkeypatch.setattr(prompt_mod.subprocess, "run", runner)
    rc, result, _ = _run(capsys, "sudo-pw", "dostal-sudo", "--surface", "zenity")
    assert rc == 0
    assert result["surface"] == "zenity"

    monkeypatch.setenv("PORTUNUS_PROMPT_SURFACE", "kdialog")
    rc, result, _ = _run(capsys, "other-pw", "dostal-other", "--surface", "t3")
    assert rc == 0
    assert result["surface"] == "t3" and result["pending"] is True


@pytest.mark.parametrize("env,surface,expected", [
    ({}, "auto", "auto"),
    ({"T3CODE_THREAD_ID": "t"}, "auto", "t3"),
    ({"T3CODE_THREAD_ID": ""}, "auto", "auto"),
    ({"T3CODE_THREAD_ID": "t", "PORTUNUS_PROMPT_SURFACE": "ui"}, "auto", "ui"),
    ({"T3CODE_THREAD_ID": "t", "PORTUNUS_PROMPT_SURFACE": "ui"}, "xterm", "xterm"),
    ({"PORTUNUS_PROMPT_SURFACE": "t3"}, "auto", "t3"),
])
def test_choose_surface(env, surface, expected):
    assert prompt_mod.choose_surface(surface, env) == expected


# --- step two: completing from the card's file ------------------------------

def test_completion_stores_audits_and_deletes_the_file(home, card, capsys):
    path = card()
    rc, result, captured = _run(
        capsys, "sudo-pw", "dostal-sudo", "--value-file", str(path), "--enable", "--gate",
    )
    assert rc == 0
    assert result == {"name": "sudo-pw", "state": "enabled", "stored": True,
                      "gated": True, "surface": "t3", "file_removed": True}
    assert not path.exists()
    assert LocalEncryptedBackend().access("dostal-sudo") == SECRET
    drops = [e for e in AuditChain().entries() if e["action"] == "drop"]
    assert [(e["secret"], e["result"]) for e in drops] == [("dostal-sudo", "stored:prompt:t3")]
    _assert_no_leak(home, captured)


def test_completion_lands_dropped_and_ungated_by_default(home, card, capsys):
    rc, result, _ = _run(capsys, "sudo-pw", "dostal-sudo", "--value-file", str(card()),
                         "--surface", "t3")
    assert rc == 0
    assert result["state"] == "dropped" and result["gated"] is False


def test_completion_strips_exactly_one_trailing_newline(home, card, capsys):
    rc, _, _ = _run(capsys, "a", "sm-a", "--value-file", str(card(SECRET + "\n\n", name="a")))
    assert rc == 0
    assert LocalEncryptedBackend().access("sm-a") == SECRET + "\n"

    rc, _, _ = _run(capsys, "b", "sm-b", "--value-file", str(card(SECRET + "\r\n", name="b")))
    assert rc == 0
    assert LocalEncryptedBackend().access("sm-b") == SECRET

    rc, _, _ = _run(capsys, "c", "sm-c", "--value-file", str(card(SECRET, name="c")))
    assert rc == 0
    assert LocalEncryptedBackend().access("sm-c") == SECRET


def test_completion_ignores_the_surface_override(home, card, monkeypatch, capsys):
    monkeypatch.setenv("PORTUNUS_PROMPT_SURFACE", "kdialog")
    rc, result, _ = _run(capsys, "sudo-pw", "dostal-sudo", "--value-file", str(card()))
    assert rc == 0 and result["surface"] == "t3"


def test_value_file_with_another_explicit_surface_is_an_error(home, card, capsys):
    path = card()
    rc, result, _ = _run(capsys, "sudo-pw", "dostal-sudo", "--value-file", str(path),
                         "--surface", "kdialog")
    assert rc == 1
    assert "cannot be combined" in result["error"]
    assert path.read_text() == SECRET + "\n"
    _assert_untouched(home)


def _relative(card, tmp_path, monkeypatch):
    path = card()
    monkeypatch.chdir(path.parent)
    return path, path.name


def _symlink(card, tmp_path, monkeypatch):
    path = card()
    link = path.parent / "link"
    link.symlink_to(path)
    return path, str(link)


def _wrong_mode(card, tmp_path, monkeypatch):
    path = card(mode=0o640)
    return path, str(path)


def _too_large(card, tmp_path, monkeypatch):
    path = card("x" * (64 * 1024 + 1))
    return path, str(path)


def _empty(card, tmp_path, monkeypatch):
    path = card("")
    return path, str(path)


def _only_a_newline(card, tmp_path, monkeypatch):
    path = card("\n")
    return path, str(path)


def _not_text(card, tmp_path, monkeypatch):
    path = card(b"\xff\xfe\x00binary")
    return path, str(path)


def _directory(card, tmp_path, monkeypatch):
    path = card().parent
    return path, str(path)


def _missing(card, tmp_path, monkeypatch):
    return None, str(card().parent / "nope")


@pytest.mark.parametrize("make,expected", [
    (_relative, "absolute path"),
    (_symlink, "symlink"),
    (_wrong_mode, "group or others"),
    (_too_large, "larger than 64 KiB"),
    (_empty, "empty"),
    (_only_a_newline, "empty secret value"),
    (_not_text, "not UTF-8"),
    (_directory, "not a regular file"),
    (_missing, "does not exist"),
])
def test_unsafe_value_file_leaves_vault_and_file_untouched(home, card, tmp_path, monkeypatch,
                                                           capsys, make, expected):
    path, argument = make(card, tmp_path, monkeypatch)
    before = (path.read_bytes(), path.stat().st_mode) if path and path.is_file() else None
    rc, result, _ = _run(capsys, "sudo-pw", "dostal-sudo", "--value-file", argument)
    assert rc == 1
    assert expected in result["error"]
    assert "stored" not in result
    if path is not None:
        assert path.exists()
    if before is not None:
        assert (path.read_bytes(), path.stat().st_mode) == before
    _assert_untouched(home)


def test_value_file_owned_by_someone_else_is_rejected():
    regular = 0o100600
    mine = SimpleNamespace(st_mode=regular, st_uid=os.getuid(), st_size=12)
    theirs = SimpleNamespace(st_mode=regular, st_uid=os.getuid() + 1, st_size=12)
    assert prompt_mod._value_file_problem(mine) == ""
    assert "not owned by the current user" in prompt_mod._value_file_problem(theirs)


def test_store_failure_leaves_the_file_for_a_retry(home, card, monkeypatch, capsys):
    path = card()

    def boom(self, sm_name, value):
        raise RuntimeError("disk full")

    monkeypatch.setattr(LocalEncryptedBackend, "store", boom)
    rc, result, captured = _run(capsys, "sudo-pw", "dostal-sudo", "--value-file", str(path))
    assert rc == 1
    assert "nothing stored" in result["error"]
    assert "left in place" in result["error"]
    assert path.read_text() == SECRET + "\n"
    assert "sudo-pw" not in Registry()  # the half-made registry entry is undone
    assert not [e for e in AuditChain().entries() if e["action"] == "drop"]
    _assert_no_leak(home, captured)


def test_undeletable_file_still_reports_stored_with_a_warning(home, card, monkeypatch, capsys):
    path = card()
    real_unlink = os.unlink

    def unlink(target, *args, **kwargs):
        if str(target) == str(path):
            raise PermissionError(13, "Permission denied")
        return real_unlink(target, *args, **kwargs)

    monkeypatch.setattr(prompt_mod.os, "unlink", unlink)
    rc, result, _ = _run(capsys, "sudo-pw", "dostal-sudo", "--value-file", str(path))
    assert rc == 0
    assert result["stored"] is True
    assert result["file_removed"] is False
    assert str(path) in result["warning"]
    assert LocalEncryptedBackend().access("dostal-sudo") == SECRET


def test_completion_refuses_non_local_backend(home, card, monkeypatch, capsys):
    path = card()
    monkeypatch.setenv("PORTUNUS_BACKEND", "mock")
    rc, result, _ = _run(capsys, "sudo-pw", "dostal-sudo", "--value-file", str(path))
    assert rc == 1
    assert "local-encrypted backend" in result["error"]
    assert path.exists()


# --- MCP --------------------------------------------------------------------

def test_mcp_two_call_handoff(home, card, monkeypatch):
    from portunus import mcp_server

    monkeypatch.setenv("T3CODE_THREAD_ID", "thread-1")
    first = mcp_server.portunus_prompt(
        name="sudo-pw", sm_name="dostal-sudo", provider="linux",
        description="sudo on workstation", enable=True, gate=True,
    )
    assert first["pending"] is True and first["surface"] == "t3"
    assert first["card"]["tool"] == "mcp__t3-code__request_secret"
    assert first["card"]["arguments"]["delivery"] == "file"
    assert "cli" not in first["then"]
    _assert_untouched(home)

    # What the agent does next: raise the card, then pass its file path on.
    path = card()
    arguments = dict(first["then"]["arguments"], value_file=str(path))
    second = getattr(mcp_server, first["then"]["tool"])(**arguments)
    assert second == {"name": "sudo-pw", "state": "enabled", "stored": True,
                      "gated": True, "surface": "t3", "file_removed": True}
    assert not path.exists()
    assert SECRET.strip() not in json.dumps([first, second])
    ref = Registry().require("sudo-pw")
    assert (ref.provider, ref.description) == ("linux", "sudo on workstation")
    assert LocalEncryptedBackend().access("dostal-sudo") == SECRET


def test_mcp_explicit_t3_surface_and_bad_file(home, card):
    from portunus import mcp_server

    assert mcp_server.portunus_prompt(name="a", sm_name="b", surface="t3")["surface"] == "t3"
    path = card(mode=0o644)
    result = mcp_server.portunus_prompt(name="a", sm_name="b", value_file=str(path))
    assert "error" in result and path.exists()
    result = mcp_server.portunus_prompt(name="a", sm_name="b", value_file=str(path),
                                        surface="zenity")
    assert "cannot be combined" in result["error"]
    _assert_untouched(home)


def test_mcp_docstring_explains_the_t3_surface():
    from portunus import mcp_server

    doc = " ".join((mcp_server.portunus_prompt.__doc__ or "").split())
    assert "mcp__t3-code__request_secret" in doc
    assert "value_file" in doc
    assert "Inside T3 Code" in doc
    assert "Never read, print or copy that file" in doc
