"""The `secrets` CLI — swarm-compatible surface over the local encrypted vault.

The load-bearing assertions: a stored value is encrypted at rest, is injected
only into a 0600 env file / child-process env / exec'd argv, and NEVER appears
in stdout, stderr, or the audit log (except the explicit, lifecycle-guarded
`secrets get`).
"""
import io
import json
import os
import stat
import sys
import time
from pathlib import Path

import pytest

from portunus import secrets_cli
from portunus.secrets_cli import env_names, main

SECRET = "lin_api_supersecret_9f8e7d6c"
GEM = "AIza-fake-gemini-key-000111"


@pytest.fixture
def cli_home(home, monkeypatch):
    monkeypatch.setenv("PORTUNUS_KEY_PROVIDER", "file")
    monkeypatch.delenv("PORTUNUS_BACKEND", raising=False)
    monkeypatch.delenv("RUNNER_TEMP", raising=False)
    return home


def run(monkeypatch, capsys, argv, stdin=None):
    if stdin is not None:
        monkeypatch.setattr(sys, "stdin", io.StringIO(stdin))
    code = main(argv)
    out = capsys.readouterr()
    return code, out.out, out.err


def store(monkeypatch, capsys, scope="att", kind="linear", value=SECRET, verb="set"):
    return run(monkeypatch, capsys, [verb, scope, kind], stdin=value + "\n")


# --- kind -> env mapping (frozen contract with dostal-swarm) ------------------
def test_env_names_match_swarm_contract():
    assert env_names("gemini") == ["GEMINI_API_KEY", "GOOGLE_API_KEY"]
    assert env_names("openai") == ["OPENAI_API_KEY"]
    assert env_names("codex") == ["OPENAI_API_KEY"]
    assert env_names("anthropic") == ["ANTHROPIC_API_KEY"]
    assert env_names("claude") == ["ANTHROPIC_API_KEY"]
    assert env_names("linear") == ["LINEAR_API_KEY"]
    assert env_names("slack") == ["SLACK_BOT_TOKEN"]
    assert env_names("github") == ["GH_TOKEN", "GITHUB_TOKEN"]
    assert env_names("locked-key") == ["LOCKED_KEY_KEY"]


# --- set / get / rm -----------------------------------------------------------
def test_set_stores_encrypted_and_registers(cli_home, monkeypatch, capsys):
    code, out, err = store(monkeypatch, capsys)
    assert code == 0
    assert "dostal-att-linear" in out and "encrypted at rest" in out
    assert SECRET not in out + err
    # encrypted at rest: value nowhere under the state home
    for root, _dirs, files in os.walk(cli_home):
        for fname in files:
            assert SECRET.encode() not in open(os.path.join(root, fname), "rb").read(), fname


def test_get_roundtrip_when_enabled(cli_home, monkeypatch, capsys):
    store(monkeypatch, capsys)
    code, out, err = run(monkeypatch, capsys, ["get", "att", "linear"])
    assert code == 0
    assert out.strip() == SECRET


def test_rm_cleans_vault_and_registry(cli_home, monkeypatch, capsys):
    store(monkeypatch, capsys)
    code, out, _ = run(monkeypatch, capsys, ["rm", "att", "linear"])
    assert code == 0
    code, _, err = run(monkeypatch, capsys, ["get", "att", "linear"])
    assert code == 1 and "unknown secret" in err
    assert not list((cli_home / "vault").glob("dostal-att-linear*"))


# --- lifecycle ----------------------------------------------------------------
def test_lock_blocks_get_but_inject_works(cli_home, monkeypatch, capsys, tmp_path):
    store(monkeypatch, capsys)
    assert run(monkeypatch, capsys, ["lock", "att", "linear"])[0] == 0
    code, out, err = run(monkeypatch, capsys, ["get", "att", "linear"])
    assert code == 1 and "inject-only" in err and SECRET not in out + err
    envfile = tmp_path / "agent.env"
    code, out, err = run(monkeypatch, capsys, ["inject", "att", "--out", str(envfile)])
    assert code == 0
    assert f"LINEAR_API_KEY={SECRET}" in envfile.read_text()


def test_dropped_blocks_inject_until_enable(cli_home, monkeypatch, capsys, tmp_path):
    store(monkeypatch, capsys, verb="drop")
    envfile = tmp_path / "agent.env"
    code, out, err = run(monkeypatch, capsys, ["inject", "att", "--out", str(envfile)])
    assert code == 1
    assert "dropped" in err and SECRET not in out + err
    assert run(monkeypatch, capsys, ["enable", "att", "linear"])[0] == 0
    assert run(monkeypatch, capsys, ["inject", "att", "--out", str(envfile)])[0] == 0


def test_revoke_blocks_everything(cli_home, monkeypatch, capsys, tmp_path):
    store(monkeypatch, capsys)
    run(monkeypatch, capsys, ["revoke", "att", "linear"])
    assert run(monkeypatch, capsys, ["get", "att", "linear"])[0] == 1
    assert run(monkeypatch, capsys, ["inject", "att", "--out", str(tmp_path / "x.env")])[0] == 1


# --- inject / env --------------------------------------------------------------
def test_inject_writes_0600_env_file_and_prints_path_only(cli_home, monkeypatch, capsys, tmp_path):
    store(monkeypatch, capsys)
    store(monkeypatch, capsys, scope="shared", kind="gemini", value=GEM)
    envfile = tmp_path / "agent.env"
    code, out, err = run(monkeypatch, capsys, ["inject", "att", "--out", str(envfile), "--ttl", "1800"])
    assert code == 0
    assert out.strip() == str(envfile)
    assert SECRET not in out + err and GEM not in out + err
    assert stat.S_IMODE(envfile.stat().st_mode) == 0o600
    text = envfile.read_text()
    # scope secrets + shared secrets, mapped kind -> env var(s)
    assert f"LINEAR_API_KEY={SECRET}" in text
    assert f"GEMINI_API_KEY={GEM}" in text and f"GOOGLE_API_KEY={GEM}" in text
    assert "PORTUNUS_SCOPE=att" in text
    assert "PORTUNUS_HANDLES=" in text and "portunus:att:linear:1" in text
    expires = int([l for l in text.splitlines() if l.startswith("PORTUNUS_EXPIRES_AT=")][0].split("=")[1])
    assert 0 < expires - int(time.time()) <= 1800


def test_scope_overrides_shared(cli_home, monkeypatch, capsys, tmp_path):
    store(monkeypatch, capsys, scope="shared", kind="linear", value="shared-linear-value")
    store(monkeypatch, capsys, scope="att", kind="linear", value=SECRET)
    envfile = tmp_path / "agent.env"
    run(monkeypatch, capsys, ["inject", "att", "--out", str(envfile)])
    assert f"LINEAR_API_KEY={SECRET}" in envfile.read_text()


def test_inject_unknown_key_fails_clearly(cli_home, monkeypatch, capsys):
    store(monkeypatch, capsys)
    code, out, err = run(monkeypatch, capsys, ["inject", "att", "--keys", "nosuchkind"])
    assert code == 1
    assert "unknown key" in err


def test_env_is_deprecated_alias(cli_home, monkeypatch, capsys, tmp_path):
    store(monkeypatch, capsys)
    envfile = tmp_path / "agent.env"
    code, out, err = run(monkeypatch, capsys, ["env", "att", "--out", str(envfile)])
    assert code == 0
    assert "legacy alias" in err
    assert f"LINEAR_API_KEY={SECRET}" in envfile.read_text()


def test_expire_check(cli_home, monkeypatch, capsys, tmp_path):
    store(monkeypatch, capsys)
    envfile = tmp_path / "agent.env"
    run(monkeypatch, capsys, ["inject", "att", "--out", str(envfile), "--ttl", "3600"])
    assert run(monkeypatch, capsys, ["expire-check", str(envfile)])[0] == 0
    stale = tmp_path / "stale.env"
    stale.write_text("PORTUNUS_EXPIRES_AT=1\n")
    assert run(monkeypatch, capsys, ["expire-check", str(stale)])[0] == 2
    nostamp = tmp_path / "nostamp.env"
    nostamp.write_text("SOME=thing\n")
    assert run(monkeypatch, capsys, ["expire-check", str(nostamp)])[0] == 2


# --- exec: values only in the child environment --------------------------------
def test_exec_injects_child_env_without_output_leak(cli_home, monkeypatch, capsys):
    store(monkeypatch, capsys)
    captured = {}

    def fake_exec(argv, env):
        captured["argv"] = list(argv)
        captured["env"] = env

    monkeypatch.setattr(secrets_cli, "_exec", fake_exec)
    code, out, err = run(monkeypatch, capsys, ["exec", "att", "--", "true", "--flag"])
    assert code == 0
    assert captured["argv"] == ["true", "--flag"]
    assert captured["env"]["LINEAR_API_KEY"] == SECRET
    assert SECRET not in out + err


# --- resolve: {{secret:NAME}} boundary substitution -----------------------------
def test_resolve_tempfile_mode_prints_path_only(cli_home, monkeypatch, capsys):
    store(monkeypatch, capsys)
    code, out, err = run(monkeypatch, capsys, ["resolve", "Bearer {{secret:att-linear}}"])
    assert code == 0
    path = Path(out.strip())
    assert path.exists()
    assert SECRET not in out + err
    assert path.read_text() == f"Bearer {SECRET}"
    assert stat.S_IMODE(path.stat().st_mode) == 0o600
    path.unlink()


def test_resolve_unknown_reference(cli_home, monkeypatch, capsys):
    code, _, err = run(monkeypatch, capsys, ["resolve", "{{secret:nope}}"])
    assert code == 1 and "unknown reference" in err


# --- handle / status / discover -------------------------------------------------
def test_handle_format_no_plaintext(cli_home, monkeypatch, capsys):
    store(monkeypatch, capsys)
    code, out, err = run(monkeypatch, capsys, ["handle", "att", "linear"])
    assert code == 0
    assert out.strip() == "portunus:att:linear:1"
    assert SECRET not in out + err


def test_status_shows_state_and_mapping(cli_home, monkeypatch, capsys):
    store(monkeypatch, capsys)
    code, out, _ = run(monkeypatch, capsys, ["status", "att", "linear"])
    assert code == 0
    assert "state:         enabled" in out
    assert "LINEAR_API_KEY" in out
    assert SECRET not in out


def test_discover_filters_and_is_value_free(cli_home, monkeypatch, capsys):
    run(monkeypatch, capsys,
        ["set", "att", "linear", "--description", "ATT Linear", "--project", "att", "--env", "prod"],
        stdin=SECRET + "\n")
    code, out, _ = run(monkeypatch, capsys, ["discover", "--project", "att", "--output", "json"])
    assert code == 0
    rows = json.loads(out)
    assert rows and rows[0]["sm_name"] == "dostal-att-linear"
    assert SECRET not in out


# --- audit: names only, chain intact ---------------------------------------------
def test_audit_never_contains_value_and_chain_verifies(cli_home, monkeypatch, capsys, tmp_path):
    store(monkeypatch, capsys)
    run(monkeypatch, capsys, ["inject", "att", "--out", str(tmp_path / "a.env")])
    run(monkeypatch, capsys, ["get", "att", "linear"])
    log = (cli_home / "audit.log").read_text()
    assert SECRET not in log
    assert '"action":"inject"' in log and '"action":"set"' in log
    code, out, _ = run(monkeypatch, capsys, ["verify"])
    assert code == 0 and "INTACT" in out


def test_mount_contract_shape(cli_home, monkeypatch, capsys):
    code, out, _ = run(monkeypatch, capsys, ["mount"])
    assert code == 0
    contract = json.loads(out)
    assert contract["tab"] == "Vault"
    assert contract["plugin"] == "portunus"
    assert set(contract["sources"]) == {"references", "status", "audit", "verify"}
