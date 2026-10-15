"""Story 01 (portunus-chrome-credential-broker): profile discovery + metadata-only
login listing. Zero decryption anywhere in this file -- chrome_discovery.py must
be structurally incapable of returning a password."""
import json
import sqlite3
import time

import pytest

from portunus.chrome_discovery import ChromeProfile, discover_profiles, list_logins


def _write_local_state(user_data_dir, profiles):
    """profiles: dict of dir_name -> display_name."""
    local_state = {"profile": {"info_cache": {name: {"name": display} for name, display in profiles.items()}}}
    (user_data_dir / "Local State").write_text(json.dumps(local_state))


def _write_login_data(path, rows):
    """rows: list of (origin_url, username_value, password_value_bytes)."""
    conn = sqlite3.connect(str(path))
    conn.execute(
        "CREATE TABLE logins (origin_url TEXT, username_value TEXT, password_value BLOB, "
        "signon_realm TEXT, date_created INTEGER)"
    )
    for origin, username, pw in rows:
        conn.execute(
            "INSERT INTO logins (origin_url, username_value, password_value, signon_realm, date_created) "
            "VALUES (?, ?, ?, ?, ?)",
            (origin, username, pw, origin, 0),
        )
    conn.commit()
    conn.close()


@pytest.fixture
def user_data_dir(tmp_path):
    d = tmp_path / "User Data"
    d.mkdir()
    return d


def test_discover_profiles_reads_local_state_display_names(user_data_dir):
    _write_local_state(user_data_dir, {"Default": "Person 1", "Profile 1": "Work"})
    (user_data_dir / "Default").mkdir()
    (user_data_dir / "Profile 1").mkdir()

    profiles = discover_profiles(user_data_dir)

    by_dir = {p.dir_name: p.display_name for p in profiles}
    assert by_dir == {"Default": "Person 1", "Profile 1": "Work"}


def test_discover_profiles_empty_when_no_chrome_installed(tmp_path):
    missing = tmp_path / "does-not-exist"
    assert discover_profiles(missing) == []


def test_discover_profiles_handles_profile_with_no_saved_logins_yet(user_data_dir):
    _write_local_state(user_data_dir, {"Default": "Person 1"})
    (user_data_dir / "Default").mkdir()
    # No Login Data file written at all for this profile.

    profiles = discover_profiles(user_data_dir)
    assert len(profiles) == 1


def test_list_logins_returns_origin_and_username_only(user_data_dir):
    profile_dir = user_data_dir / "Default"
    profile_dir.mkdir()
    _write_login_data(
        profile_dir / "Login Data",
        [
            ("https://example.com", "alice@example.com", b"v10encryptedblob1"),
            ("https://other.com", "bob@other.com", b"v10encryptedblob2"),
            ("https://third.com", "carol@third.com", b"v10encryptedblob3"),
        ],
    )
    profile = ChromeProfile(dir_name="Default", display_name="Person 1", user_data_dir=user_data_dir)

    logins = list_logins(profile)

    assert len(logins) == 3
    pairs = {(l.origin_url, l.username_value) for l in logins}
    assert pairs == {
        ("https://example.com", "alice@example.com"),
        ("https://other.com", "bob@other.com"),
        ("https://third.com", "carol@third.com"),
    }


def test_list_logins_never_selects_password_value_column(user_data_dir, monkeypatch):
    """Structural check: the SQL text itself must never mention password_value,
    not just 'the current return value happens to omit it'."""
    profile_dir = user_data_dir / "Default"
    profile_dir.mkdir()
    _write_login_data(profile_dir / "Login Data", [("https://example.com", "alice", b"blob")])
    profile = ChromeProfile(dir_name="Default", display_name="Person 1", user_data_dir=user_data_dir)

    captured_sql = []
    real_connect = sqlite3.connect

    def spying_connect(*args, **kwargs):
        conn = real_connect(*args, **kwargs)
        conn.set_trace_callback(captured_sql.append)
        return conn

    monkeypatch.setattr(sqlite3, "connect", spying_connect)

    list_logins(profile)

    assert captured_sql, "expected at least one query to run"
    for sql in captured_sql:
        assert "password_value" not in sql.lower()


def test_list_logins_returns_empty_list_when_no_login_data_file(user_data_dir):
    profile_dir = user_data_dir / "Default"
    profile_dir.mkdir()
    # Deliberately no "Login Data" file.
    profile = ChromeProfile(dir_name="Default", display_name="Person 1", user_data_dir=user_data_dir)

    assert list_logins(profile) == []


def test_list_logins_reads_a_copy_not_the_live_locked_file(user_data_dir):
    """Holds an exclusive write lock on the real Login Data file for the
    duration of list_logins() -- proves the implementation copied it first
    rather than opening the live file, the same way Chrome itself might hold
    it while running."""
    profile_dir = user_data_dir / "Default"
    profile_dir.mkdir()
    login_data_path = profile_dir / "Login Data"
    _write_login_data(login_data_path, [("https://example.com", "alice", b"blob")])
    profile = ChromeProfile(dir_name="Default", display_name="Person 1", user_data_dir=user_data_dir)

    locker = sqlite3.connect(str(login_data_path))
    locker.execute("BEGIN EXCLUSIVE")
    try:
        logins = list_logins(profile)
        assert len(logins) == 1
    finally:
        locker.rollback()
        locker.close()
