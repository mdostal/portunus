"""portunus update -- CLI self-update.

Elevated stakes versus a normal CLI's self-update: this tool already holds
real vault access, run standalone and often (a local key-value store, per
the design goal). The same "never a silent unattended swap" posture the
desktop app's own ``updater.rs`` established applies here, plus one thing
the desktop app doesn't need: the passive/background check running on every
invocation is STRUCTURALLY incapable of installing anything. Only the
explicit ``portunus update run`` command (a human- or ``--yes``-confirmed
action) may ever call ``apply_update()`` -- see
``test_the_passive_notify_path_can_never_reach_apply_update``.

Zero secret-boundary surface, by construction: no import of
``Registry``/``Broker``/``Resolver``/``SecretBackend`` anywhere in this
module (verified structurally in tests/test_update.py) -- the update path
is exactly the part of this codebase least entitled to touch a vault.
"""
from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
import time
from pathlib import Path
from typing import Dict, Optional

from .paths import home

REPO = "mdostal/portunus"
CHECK_INTERVAL_SECONDS = 24 * 60 * 60


def _run(argv, timeout: int = 15) -> Optional[subprocess.CompletedProcess]:
    try:
        return subprocess.run(argv, capture_output=True, text=True, timeout=timeout)
    except (OSError, subprocess.TimeoutExpired):
        return None


def _parse_semver(value: str) -> tuple:
    raw = value.strip().lstrip("v")
    parts = raw.split(".")
    if len(parts) != 3:
        raise ValueError(f"not a plain X.Y.Z version: {value!r}")
    try:
        return tuple(int(p) for p in parts)
    except ValueError:
        raise ValueError(f"not a plain X.Y.Z version: {value!r}") from None


def is_newer(current: str, latest_tag: str) -> bool:
    """Errors loudly on either failing to parse -- never silently treats
    malformed input as "not newer" (mirrors updater.rs's own Result<bool,
    String>, for the same reason: silently swallowing a parse failure here
    could mean silently never telling the operator about a real update)."""
    return _parse_semver(latest_tag) > _parse_semver(current)


def latest_release_tag() -> Optional[str]:
    """The user's own already-authenticated `gh` CLI, never a token this
    tool holds itself -- same posture as the desktop app's updater.rs."""
    # No tag argument -- `gh release view <tag>` treats `<tag>` as a literal
    # release tag to look up (a literal "latest" fails with "release not
    # found" unless a release happens to be tagged that). Omitting the
    # argument entirely is what actually means "show the latest release."
    result = _run(["gh", "release", "view", "--repo", REPO, "--json", "tagName", "--jq", ".tagName"])
    if result is None or result.returncode != 0:
        return None
    tag = result.stdout.strip()
    return tag or None


def _cache_path(home_dir: Optional[Path]) -> Path:
    base = home_dir if home_dir is not None else home()
    return base / "update-check.json"


def cached_status(home_dir: Optional[Path] = None) -> Optional[Dict[str, object]]:
    path = _cache_path(home_dir)
    if not path.is_file():
        return None
    try:
        return json.loads(path.read_text())
    except (OSError, ValueError):
        return None


def should_check(home_dir: Optional[Path] = None) -> bool:
    cached = cached_status(home_dir)
    if cached is None:
        return True
    checked_at = cached.get("checked_at", 0)
    return (time.time() - checked_at) >= CHECK_INTERVAL_SECONDS


def check_now(current: Optional[str] = None, home_dir: Optional[Path] = None) -> Dict[str, object]:
    """Always a LIVE check -- never trusts a stale cache for the result it
    returns (`update run` depends on this being accurate every time it's
    called, not a possibly-hours-old cached guess). Writes the result to
    the cache purely so *other* invocations' passive notice has something
    to read."""
    if current is None:
        from . import __version__ as current
    tag = latest_release_tag()
    if tag is None:
        result = {
            "current": current, "latest": None, "update_available": None,
            "checked_at": time.time(), "error": "could not reach GitHub releases (is `gh` installed and authenticated?)",
        }
    else:
        try:
            available = is_newer(current, tag)
            error = None
        except ValueError as e:
            available = None
            error = str(e)
        result = {"current": current, "latest": tag, "update_available": available, "checked_at": time.time(), "error": error}
    path = _cache_path(home_dir)
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(".json.tmp")
    tmp.write_text(json.dumps(result))
    os.replace(tmp, path)
    return result


def _is_dev_checkout(start: Path) -> bool:
    """True if `start` lives inside a git working tree -- an editable/dev
    install, which `update run` must never overwrite. A real pipx/pip
    install's site-packages lives nowhere near a `.git` directory."""
    current = start.resolve()
    for _ in range(8):
        if (current / ".git").exists():
            return True
        if current.parent == current:
            break
        current = current.parent
    return False


def is_dev_checkout() -> bool:
    return _is_dev_checkout(Path(__file__).parent)


def apply_update(tag: str) -> bool:
    """The one mutating path. Pins to the EXACT resolved release tag (never
    a floating `main` HEAD) so what gets installed is always a specific,
    auditable commit. Prefers pipx (matches scripts/install.sh); falls back
    to the current interpreter's pip when pipx isn't present."""
    spec = f"git+https://github.com/{REPO}.git@{tag}"
    if shutil.which("pipx"):
        argv = ["pipx", "install", "--force", spec]
    else:
        argv = [sys.executable, "-m", "pip", "install", "--upgrade", "--force-reinstall", spec]
    result = subprocess.run(argv)
    return result.returncode == 0


def _should_skip_passive_check() -> bool:
    return "PYTEST_CURRENT_TEST" in os.environ or bool(os.environ.get("PORTUNUS_NO_UPDATE_CHECK"))


def maybe_notify(home_dir: Optional[Path] = None) -> None:
    """Called once per CLI invocation. Read-only from this process's point
    of view: at most spawns a detached background process to do a live
    check (never waits on it, never touches its output), and prints at
    most one line to STDERR (never stdout -- would corrupt scripted/--json
    output) if a PREVIOUSLY cached check found something newer. Cannot
    install anything -- the installer function is never referenced here at
    all, verified structurally by this module's own test suite."""
    if _should_skip_passive_check():
        return
    if should_check(home_dir):
        subprocess.Popen(
            [sys.executable, "-c", "from portunus.update import check_now; check_now()"],
            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, stdin=subprocess.DEVNULL,
            start_new_session=True,
        )
    cached = cached_status(home_dir)
    if cached and cached.get("update_available"):
        print(
            f"note: portunus {cached['latest']} is available (you have {cached['current']}) "
            "-- run `portunus update run` to upgrade",
            file=sys.stderr,
        )
