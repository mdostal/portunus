"""Chrome saved-login discovery (portunus-chrome-credential-broker, Story 01).

Metadata only. This module never selects, stores, or returns Chrome's
``password_value`` column -- it is architecturally incapable of exposing a
secret. Decryption lives entirely in ``chrome_crypto.py``; the metadata-only
CLI commands (``portunus browser profiles``/``list``) call straight into this
module and never import ``chrome_crypto``, so that end-to-end surface stays
structurally unable to leak a password.

``Login Data`` is always copied to a temp file before it's opened -- Chrome
may hold it locked while running, and Portunus must never risk a write-mode
open against a file the real browser owns.
"""
from __future__ import annotations

import json
import shutil
import sqlite3
import subprocess
import sys
import tempfile
from dataclasses import dataclass
from pathlib import Path
from typing import List


def _default_user_data_dir() -> Path:
    home = Path.home()
    if sys.platform == "darwin":
        return home / "Library" / "Application Support" / "Google" / "Chrome"
    return home / ".config" / "google-chrome"


@dataclass(frozen=True)
class ChromeProfile:
    dir_name: str
    display_name: str
    user_data_dir: Path


@dataclass(frozen=True)
class ChromeLogin:
    origin_url: str
    username_value: str


def discover_profiles(user_data_dir: Path | None = None) -> List[ChromeProfile]:
    """List Chrome's profiles from Local State's profile.info_cache.

    Returns an empty list when Chrome isn't installed (no User Data dir) or
    Local State is missing/unreadable -- "no Chrome" is a normal, non-fatal
    state for every caller (CLI, ChromeBackend), never an error.
    """
    if user_data_dir is None:
        user_data_dir = _default_user_data_dir()
    user_data_dir = Path(user_data_dir)

    local_state_path = user_data_dir / "Local State"
    if not local_state_path.is_file():
        return []

    try:
        local_state = json.loads(local_state_path.read_text())
    except (OSError, json.JSONDecodeError):
        return []

    info_cache = local_state.get("profile", {}).get("info_cache", {})
    profiles = []
    for dir_name, info in info_cache.items():
        display_name = info.get("name", dir_name) if isinstance(info, dict) else dir_name
        profiles.append(
            ChromeProfile(dir_name=dir_name, display_name=display_name, user_data_dir=user_data_dir)
        )
    return profiles


def list_logins(profile: ChromeProfile) -> List[ChromeLogin]:
    """List (origin_url, username_value) pairs for a profile's saved logins.

    Never selects password_value -- see module docstring. Operates on a
    temp-file copy of Login Data, never the live file, since Chrome may hold
    it open while running.
    """
    login_data_path = profile.user_data_dir / profile.dir_name / "Login Data"
    if not login_data_path.is_file():
        return []

    with tempfile.TemporaryDirectory(prefix="portunus-chrome-") as tmp_dir:
        tmp_copy = Path(tmp_dir) / "Login Data"
        shutil.copy2(login_data_path, tmp_copy)

        conn = sqlite3.connect(str(tmp_copy))
        try:
            cursor = conn.execute("SELECT origin_url, username_value FROM logins")
            rows = cursor.fetchall()
        finally:
            conn.close()

    return [ChromeLogin(origin_url=row[0], username_value=row[1]) for row in rows]


def _process_running(name: str) -> bool:
    try:
        result = subprocess.run(["pgrep", "-x", name], capture_output=True, text=True)
    except FileNotFoundError:
        return False
    return result.returncode == 0


def classify_store(profile: ChromeProfile) -> str:
    """Best-effort, side-effect-free label for which OS secret store likely
    protects this profile's saved logins -- display only, for `portunus
    browser profiles`. Never touches chrome_crypto and never fetches any
    key or secret material: on Linux this only checks for a running keyring
    daemon process, the same desktop-environment signal Chromium itself
    uses to auto-select a backend (research-brief.md Sec 1), not a probe of
    stored data. ChromeBackend.access() is the real, authoritative decrypt
    path -- this is a label, not a guarantee of which key it will use.
    """
    if sys.platform == "darwin":
        return "Keychain"
    if _process_running("kwalletd6") or _process_running("kwalletd5") or _process_running("kwalletd"):
        return "KWallet"
    if _process_running("gnome-keyring-daemon"):
        return "Secret Service"
    return "unprotected -- no keyring"
