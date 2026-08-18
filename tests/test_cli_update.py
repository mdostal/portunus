"""`portunus update check`/`run` -- CLI surface over update.py."""
import ast
import inspect
import json

from portunus import update
from portunus.cli import cmd_update_check, cmd_update_run, main


def test_update_check_text_output_up_to_date(monkeypatch, capsys):
    monkeypatch.setattr(
        update, "check_now",
        lambda **k: {"current": "0.26.0", "latest": "v0.26.0", "update_available": False, "checked_at": 0, "error": None},
    )
    rc = main(["update", "check"])
    out = capsys.readouterr().out
    assert rc == 0
    assert "up to date" in out


def test_update_check_text_output_available(monkeypatch, capsys):
    monkeypatch.setattr(
        update, "check_now",
        lambda **k: {"current": "0.26.0", "latest": "v0.99.0", "update_available": True, "checked_at": 0, "error": None},
    )
    rc = main(["update", "check"])
    out = capsys.readouterr().out
    assert rc == 0
    assert "v0.99.0" in out
    assert "update run" in out


def test_update_check_json_flag(monkeypatch, capsys):
    payload = {"current": "0.26.0", "latest": "v0.99.0", "update_available": True, "checked_at": 0, "error": None}
    monkeypatch.setattr(update, "check_now", lambda **k: payload)
    rc = main(["update", "check", "--json"])
    out = capsys.readouterr().out
    assert rc == 0
    assert json.loads(out) == payload


def test_update_check_reports_error_and_nonzero(monkeypatch, capsys):
    monkeypatch.setattr(
        update, "check_now",
        lambda **k: {"current": "0.26.0", "latest": None, "update_available": None, "checked_at": 0, "error": "no gh"},
    )
    rc = main(["update", "check"])
    out = capsys.readouterr().out
    assert rc == 1
    assert "no gh" in out


def test_update_run_refuses_on_dev_checkout(monkeypatch, capsys):
    monkeypatch.setattr(update, "is_dev_checkout", lambda: True)
    rc = main(["update", "run", "--yes"])
    out = capsys.readouterr().out
    assert rc == 1
    assert "dev/editable install" in out


def test_update_run_noop_when_already_up_to_date(monkeypatch, capsys):
    monkeypatch.setattr(update, "is_dev_checkout", lambda: False)
    monkeypatch.setattr(
        update, "check_now",
        lambda **k: {"current": "0.26.0", "latest": "v0.26.0", "update_available": False, "checked_at": 0, "error": None},
    )
    rc = main(["update", "run", "--yes"])
    out = capsys.readouterr().out
    assert rc == 0
    assert "already up to date" in out


def test_update_run_reports_check_error(monkeypatch, capsys):
    monkeypatch.setattr(update, "is_dev_checkout", lambda: False)
    monkeypatch.setattr(
        update, "check_now",
        lambda **k: {"current": "0.26.0", "latest": None, "update_available": None, "checked_at": 0, "error": "no gh"},
    )
    rc = main(["update", "run", "--yes"])
    assert rc == 1
    assert "no gh" in capsys.readouterr().out


def test_update_run_installs_when_yes_passed(monkeypatch, capsys):
    monkeypatch.setattr(update, "is_dev_checkout", lambda: False)
    monkeypatch.setattr(
        update, "check_now",
        lambda **k: {"current": "0.26.0", "latest": "v0.27.0", "update_available": True, "checked_at": 0, "error": None},
    )
    seen = {}
    monkeypatch.setattr(update, "apply_update", lambda tag: seen.setdefault("tag", tag) or True)
    rc = main(["update", "run", "--yes"])
    out = capsys.readouterr().out
    assert rc == 0
    assert seen["tag"] == "v0.27.0"
    assert "updated to v0.27.0" in out


def test_update_run_reports_install_failure(monkeypatch, capsys):
    monkeypatch.setattr(update, "is_dev_checkout", lambda: False)
    monkeypatch.setattr(
        update, "check_now",
        lambda **k: {"current": "0.26.0", "latest": "v0.27.0", "update_available": True, "checked_at": 0, "error": None},
    )
    monkeypatch.setattr(update, "apply_update", lambda tag: False)
    rc = main(["update", "run", "--yes"])
    assert rc == 1
    assert "failed" in capsys.readouterr().out


def test_update_run_without_yes_refuses_non_interactively(monkeypatch, capsys):
    monkeypatch.setattr(update, "is_dev_checkout", lambda: False)
    monkeypatch.setattr(
        update, "check_now",
        lambda **k: {"current": "0.26.0", "latest": "v0.27.0", "update_available": True, "checked_at": 0, "error": None},
    )
    monkeypatch.setattr(update.sys.stdin, "isatty", lambda: False)
    apply_calls = []
    monkeypatch.setattr(update, "apply_update", lambda tag: apply_calls.append(tag) or True)
    rc = main(["update", "run"])
    out = capsys.readouterr().out
    assert rc == 1
    assert "--yes" in out
    assert apply_calls == []  # never installed without explicit confirmation


def test_update_run_declines_on_interactive_no(monkeypatch, capsys):
    monkeypatch.setattr(update, "is_dev_checkout", lambda: False)
    monkeypatch.setattr(
        update, "check_now",
        lambda **k: {"current": "0.26.0", "latest": "v0.27.0", "update_available": True, "checked_at": 0, "error": None},
    )
    monkeypatch.setattr(update.sys.stdin, "isatty", lambda: True)
    monkeypatch.setattr("builtins.input", lambda prompt: "n")
    apply_calls = []
    monkeypatch.setattr(update, "apply_update", lambda tag: apply_calls.append(tag) or True)
    rc = main(["update", "run"])
    assert rc == 0
    assert "not updating" in capsys.readouterr().out
    assert apply_calls == []


def test_update_run_accepts_on_interactive_yes(monkeypatch, capsys):
    monkeypatch.setattr(update, "is_dev_checkout", lambda: False)
    monkeypatch.setattr(
        update, "check_now",
        lambda **k: {"current": "0.26.0", "latest": "v0.27.0", "update_available": True, "checked_at": 0, "error": None},
    )
    monkeypatch.setattr(update.sys.stdin, "isatty", lambda: True)
    monkeypatch.setattr("builtins.input", lambda prompt: "y")
    apply_calls = []
    monkeypatch.setattr(update, "apply_update", lambda tag: apply_calls.append(tag) or True)
    rc = main(["update", "run"])
    assert rc == 0
    assert apply_calls == ["v0.27.0"]


def test_cmd_update_functions_never_touch_the_secret_boundary():
    forbidden = ("Registry", "Broker", "Resolver", "backend", "resolver", "value")
    for fn in (cmd_update_check, cmd_update_run):
        src = inspect.getsource(fn)
        code = ast.unparse(ast.parse(src))
        for term in forbidden:
            assert term not in code, f"{fn.__name__} references {term!r} -- this command should never touch the vault"
