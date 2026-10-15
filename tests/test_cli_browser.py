"""Story 04 (portunus-chrome-credential-broker): `portunus browser profiles`
and `portunus browser list --profile` -- both must be structurally
incapable of printing a password: their code paths call chrome_discovery
directly and never import or call chrome_crypto at all.
"""
import json
import sqlite3
from argparse import Namespace

import pytest

from portunus import chrome_crypto, cli


def _write_local_state(user_data_dir, profiles):
    local_state = {"profile": {"info_cache": {name: {"name": display} for name, display in profiles.items()}}}
    (user_data_dir / "Local State").write_text(json.dumps(local_state))


def _write_login_data(path, rows):
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
def fake_chrome_env(monkeypatch, tmp_path):
    user_data_dir = tmp_path / "User Data"
    user_data_dir.mkdir()
    _write_local_state(user_data_dir, {"Default": "Person 1"})
    (user_data_dir / "Default").mkdir()
    _write_login_data(
        user_data_dir / "Default" / "Login Data",
        [("https://example.com", "alice@example.com", b"v10realbloboftherealpassword")],
    )
    monkeypatch.setattr("portunus.chrome_discovery._default_user_data_dir", lambda: user_data_dir)
    return user_data_dir


def _never_call(*_args, **_kwargs):
    raise AssertionError("portunus browser profiles/list must never call chrome_crypto")


@pytest.fixture(autouse=True)
def forbid_chrome_crypto(monkeypatch):
    """If either CLI command path so much as calls into chrome_crypto, fail
    loudly -- this is the structural guarantee, not a check on output."""
    monkeypatch.setattr(chrome_crypto, "macos_key", _never_call)
    monkeypatch.setattr(chrome_crypto, "linux_key", _never_call)
    monkeypatch.setattr(chrome_crypto, "decrypt_blob", _never_call)


def test_browser_profiles_lists_profiles_with_zero_password_material(fake_chrome_env, capsys):
    rc = cli.cmd_browser_profiles(Namespace(json=False))

    assert rc == 0
    out = capsys.readouterr().out
    assert "Default" in out
    assert "Person 1" in out
    assert "realbloboftherealpassword" not in out


def test_browser_profiles_json_mode_has_no_password_field(fake_chrome_env, capsys):
    rc = cli.cmd_browser_profiles(Namespace(json=True))

    assert rc == 0
    rows = json.loads(capsys.readouterr().out)
    assert rows == [{"dir_name": "Default", "display_name": "Person 1", "store": "Keychain"}] or all(
        "password" not in r and "value" not in r for r in rows
    )


def test_browser_list_returns_origin_username_pairs_only(fake_chrome_env, capsys):
    rc = cli.cmd_browser_list(Namespace(profile="Default", json=False))

    assert rc == 0
    out = capsys.readouterr().out
    assert "https://example.com" in out
    assert "alice@example.com" in out
    assert "realbloboftherealpassword" not in out


def test_browser_list_json_mode_has_no_password_field(fake_chrome_env, capsys):
    rc = cli.cmd_browser_list(Namespace(profile="Default", json=True))

    assert rc == 0
    rows = json.loads(capsys.readouterr().out)
    assert rows == [{"origin_url": "https://example.com", "username_value": "alice@example.com"}]


def test_browser_list_unknown_profile_is_a_clean_error_not_a_crash(fake_chrome_env, capsys):
    rc = cli.cmd_browser_list(Namespace(profile="NoSuchProfile", json=False))

    assert rc != 0
