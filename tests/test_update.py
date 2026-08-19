"""portunus update -- CLI self-update. Elevated stakes vs. a normal CLI's
self-update: this tool already holds real vault access, so the update path
gets the same "never a silent unattended swap" posture the desktop app's own
updater.rs established, plus one thing the desktop app doesn't need: the
passive/background check must be STRUCTURALLY incapable of installing
anything -- only the explicit `update run` path may ever call apply_update()."""
import json
import subprocess
import time as time_module
from pathlib import Path

import pytest

from portunus import update


# --- semver compare (mirrors updater.rs's own test table) -----------------

def test_newer_tag_is_newer():
    assert update.is_newer("0.25.0", "v0.26.0") is True


def test_equal_tag_is_not_newer():
    assert update.is_newer("0.25.0", "v0.25.0") is False


def test_older_tag_is_not_newer():
    assert update.is_newer("0.26.0", "v0.25.0") is False


def test_tag_without_v_prefix_still_parses():
    assert update.is_newer("0.25.0", "0.26.0") is True


def test_malformed_current_raises():
    with pytest.raises(ValueError):
        update.is_newer("not-a-version", "v0.26.0")


def test_malformed_tag_raises():
    with pytest.raises(ValueError):
        update.is_newer("0.25.0", "not-a-version")


# --- latest_release_tag() -- gh-CLI backed, same posture as the desktop app -

def test_latest_release_tag_calls_gh_with_correct_args(monkeypatch):
    seen = {}

    def fake_run(argv, **kwargs):
        seen["argv"] = argv
        return subprocess.CompletedProcess(argv, 0, stdout="v0.26.0\n", stderr="")

    monkeypatch.setattr(update, "_run", fake_run)
    assert update.latest_release_tag() == "v0.26.0"
    assert seen["argv"] == [
        "gh", "release", "view", "--repo", update.REPO,
        "--json", "tagName", "--jq", ".tagName",
    ]


def test_latest_release_tag_none_on_failure(monkeypatch):
    monkeypatch.setattr(update, "_run", lambda argv, **kwargs: None)
    assert update.latest_release_tag() is None


def test_latest_release_tag_none_on_nonzero_exit(monkeypatch):
    monkeypatch.setattr(
        update, "_run",
        lambda argv, **kwargs: subprocess.CompletedProcess(argv, 1, stdout="", stderr="not found"),
    )
    assert update.latest_release_tag() is None


def test_latest_release_tag_none_on_empty_output(monkeypatch):
    monkeypatch.setattr(
        update, "_run",
        lambda argv, **kwargs: subprocess.CompletedProcess(argv, 0, stdout="\n", stderr=""),
    )
    assert update.latest_release_tag() is None


# --- check_now() -- always a live check, writes the cache for other invocations

def test_check_now_reports_update_available(monkeypatch, tmp_path):
    monkeypatch.setattr(update, "latest_release_tag", lambda: "v0.99.0")
    result = update.check_now(current="0.26.0", home_dir=tmp_path)
    assert result["update_available"] is True
    assert result["latest"] == "v0.99.0"
    assert result["current"] == "0.26.0"
    assert result["error"] is None


def test_check_now_reports_up_to_date(monkeypatch, tmp_path):
    monkeypatch.setattr(update, "latest_release_tag", lambda: "v0.26.0")
    result = update.check_now(current="0.26.0", home_dir=tmp_path)
    assert result["update_available"] is False


def test_check_now_handles_gh_failure_without_raising(monkeypatch, tmp_path):
    monkeypatch.setattr(update, "latest_release_tag", lambda: None)
    result = update.check_now(current="0.26.0", home_dir=tmp_path)
    assert result["update_available"] is None
    assert result["error"]


def test_check_now_writes_a_readable_cache(monkeypatch, tmp_path):
    monkeypatch.setattr(update, "latest_release_tag", lambda: "v0.99.0")
    update.check_now(current="0.26.0", home_dir=tmp_path)
    cache = json.loads((tmp_path / "update-check.json").read_text())
    assert cache["update_available"] is True
    assert cache["latest"] == "v0.99.0"
    assert "checked_at" in cache


def test_cached_status_reads_back_what_check_now_wrote(monkeypatch, tmp_path):
    monkeypatch.setattr(update, "latest_release_tag", lambda: "v0.99.0")
    update.check_now(current="0.26.0", home_dir=tmp_path)
    assert update.cached_status(home_dir=tmp_path)["latest"] == "v0.99.0"


def test_cached_status_none_when_no_cache(tmp_path):
    assert update.cached_status(home_dir=tmp_path) is None


# --- should_check() throttling ---------------------------------------------

def test_should_check_true_when_no_cache(tmp_path):
    assert update.should_check(home_dir=tmp_path) is True


def test_should_check_false_within_interval(monkeypatch, tmp_path):
    monkeypatch.setattr(update, "latest_release_tag", lambda: "v0.26.0")
    update.check_now(current="0.26.0", home_dir=tmp_path)
    assert update.should_check(home_dir=tmp_path) is False


def test_should_check_true_after_interval_elapses(monkeypatch, tmp_path):
    monkeypatch.setattr(update, "latest_release_tag", lambda: "v0.26.0")
    update.check_now(current="0.26.0", home_dir=tmp_path)
    future = time_module.time() + update.CHECK_INTERVAL_SECONDS + 1
    monkeypatch.setattr(update.time, "time", lambda: future)
    assert update.should_check(home_dir=tmp_path) is True


# --- dev-checkout detection --------------------------------------------------

def test_is_dev_checkout_true_inside_a_git_repo(tmp_path):
    (tmp_path / "repo" / ".git").mkdir(parents=True)
    pkg_dir = tmp_path / "repo" / "src" / "portunus"
    pkg_dir.mkdir(parents=True)
    assert update._is_dev_checkout(pkg_dir) is True


def test_is_dev_checkout_false_outside_a_git_repo(tmp_path):
    pkg_dir = tmp_path / "site-packages" / "portunus"
    pkg_dir.mkdir(parents=True)
    assert update._is_dev_checkout(pkg_dir) is False


# --- passive check: notify-only, never installs -----------------------------

def test_should_skip_passive_check_true_during_pytest():
    # PYTEST_CURRENT_TEST is genuinely set right now -- this is the real
    # guard that protects the rest of the test suite from ever spawning a
    # background update check.
    assert update._should_skip_passive_check() is True


def test_should_skip_passive_check_true_when_env_var_set(monkeypatch):
    monkeypatch.delenv("PYTEST_CURRENT_TEST", raising=False)
    monkeypatch.setenv("PORTUNUS_NO_UPDATE_CHECK", "1")
    assert update._should_skip_passive_check() is True


def test_should_skip_passive_check_false_when_neither_set(monkeypatch):
    monkeypatch.delenv("PYTEST_CURRENT_TEST", raising=False)
    monkeypatch.delenv("PORTUNUS_NO_UPDATE_CHECK", raising=False)
    assert update._should_skip_passive_check() is False


def test_maybe_notify_noop_when_should_skip(monkeypatch, tmp_path):
    monkeypatch.setattr(update, "_should_skip_passive_check", lambda: True)
    monkeypatch.setattr(update.subprocess, "Popen", lambda *a, **k: (_ for _ in ()).throw(AssertionError("spawned")))
    update.maybe_notify(home_dir=tmp_path)  # would raise via the poisoned Popen if it ever spawned


def test_maybe_notify_spawns_detached_background_check_when_stale(monkeypatch, tmp_path):
    monkeypatch.setattr(update, "_should_skip_passive_check", lambda: False)
    spawned = {}

    class FakePopen:
        def __init__(self, argv, **kwargs):
            spawned["argv"] = argv
            spawned["kwargs"] = kwargs

    monkeypatch.setattr(update.subprocess, "Popen", FakePopen)
    update.maybe_notify(home_dir=tmp_path)
    assert spawned["argv"][0] == update.sys.executable
    # fire-and-forget: stdio silenced, not attached to this process's session
    assert spawned["kwargs"].get("stdout") == update.subprocess.DEVNULL
    assert spawned["kwargs"].get("stderr") == update.subprocess.DEVNULL


def test_maybe_notify_prints_stderr_notice_when_cache_says_available(monkeypatch, tmp_path, capsys):
    monkeypatch.setattr(update, "_should_skip_passive_check", lambda: False)
    monkeypatch.setattr(update, "latest_release_tag", lambda: "v0.99.0")
    update.check_now(current="0.26.0", home_dir=tmp_path)  # seeds the cache
    monkeypatch.setattr(update.subprocess, "Popen", lambda *a, **k: None)  # don't actually spawn again
    update.maybe_notify(home_dir=tmp_path)
    captured = capsys.readouterr()
    assert captured.out == ""  # never stdout -- would corrupt scripted/--json output
    assert "0.99.0" in captured.err


def test_maybe_notify_silent_when_up_to_date(monkeypatch, tmp_path, capsys):
    monkeypatch.setattr(update, "_should_skip_passive_check", lambda: False)
    monkeypatch.setattr(update, "latest_release_tag", lambda: "v0.26.0")
    update.check_now(current="0.26.0", home_dir=tmp_path)
    monkeypatch.setattr(update.subprocess, "Popen", lambda *a, **k: None)
    update.maybe_notify(home_dir=tmp_path)
    assert capsys.readouterr().err == ""


def test_the_passive_notify_path_can_never_reach_apply_update():
    """Structural, not just behavioral: maybe_notify() and everything it
    calls must have zero code path to apply_update -- only cmd_update_run
    (an explicit, confirmed CLI action) may call it."""
    import ast
    import inspect

    src = inspect.getsource(update.maybe_notify)
    tree = ast.parse(src)
    code = ast.unparse(tree)
    assert "apply_update" not in code


# --- apply_update() -- the one mutating path, pinned + explicit -------------

def test_apply_update_pins_to_the_exact_tag_via_pipx(monkeypatch):
    monkeypatch.setattr(update.shutil, "which", lambda name: "/usr/local/bin/pipx" if name == "pipx" else None)
    seen = {}

    def fake_run(argv, **kwargs):
        seen["argv"] = argv
        return subprocess.CompletedProcess(argv, 0)

    monkeypatch.setattr(update.subprocess, "run", fake_run)
    assert update.apply_update("v0.27.0") is True
    assert seen["argv"] == [
        "pipx", "install", "--force", f"git+https://github.com/{update.REPO}.git@v0.27.0",
    ]


def test_apply_update_falls_back_to_pip_when_pipx_missing(monkeypatch):
    monkeypatch.setattr(update.shutil, "which", lambda name: None)
    seen = {}

    def fake_run(argv, **kwargs):
        seen["argv"] = argv
        return subprocess.CompletedProcess(argv, 0)

    monkeypatch.setattr(update.subprocess, "run", fake_run)
    assert update.apply_update("v0.27.0") is True
    assert seen["argv"] == [
        update.sys.executable, "-m", "pip", "install", "--upgrade", "--force-reinstall",
        f"git+https://github.com/{update.REPO}.git@v0.27.0",
    ]


def test_apply_update_false_on_failed_install(monkeypatch):
    monkeypatch.setattr(update.shutil, "which", lambda name: "/usr/local/bin/pipx")
    monkeypatch.setattr(update.subprocess, "run", lambda argv, **kwargs: subprocess.CompletedProcess(argv, 1))
    assert update.apply_update("v0.27.0") is False


def test_update_module_never_touches_the_secret_boundary():
    """This is exactly the code most worth attacking if the update path were
    ever compromised -- it must have zero legitimate reason to import the
    vault machinery, verified structurally, not just by omission."""
    import ast
    import inspect

    tree = ast.parse(inspect.getsource(update))
    imported_names = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            imported_names.update(alias.name for alias in node.names)
        elif isinstance(node, ast.ImportFrom):
            imported_names.update(alias.name for alias in node.names)
    forbidden = {"Registry", "Broker", "Resolver", "SecretBackend"}
    assert not (imported_names & forbidden)
