"""CLI: identity-token mint and anthropic-federation. Tokens never reach stdout/stderr."""
import base64
import json
import stat
import subprocess
import sys
import time

from portunus.cli import main

ORG = "11111111-2222-3333-4444-555555555555"


def make_jwt(**over):
    now = int(time.time())
    claims = {"iss": "https://accounts.google.com", "aud": "https://api.anthropic.com", "sub": "42",
              "email": "claude@p.iam.gserviceaccount.com", "iat": now, "exp": now + 3600}
    claims.update(over)

    def b64(obj):
        return base64.urlsafe_b64encode(json.dumps(obj).encode()).decode().rstrip("=")
    return f"{b64({'alg': 'RS256', 'kid': 'k'})}.{b64(claims)}.SECRET-SIGNATURE"


def fake_gcloud(monkeypatch, jwt, calls=None):
    def run(cmd, **kw):
        if calls is not None:
            calls.append(cmd)
        return subprocess.CompletedProcess(cmd, 0, jwt + "\n", "")
    monkeypatch.setattr("portunus.identity_token.subprocess.run", run)
    monkeypatch.setattr("portunus.identity_token.shutil.which", lambda _: "/usr/bin/gcloud")


def _set(name="mathew", *extra):
    return main(["anthropic-federation", "set", name, "--organization-id", ORG, "--rule-id", "fdrl_r",
                 "--service-account-id", "svac_s", "--impersonate-service-account",
                 "claude@p.iam.gserviceaccount.com", *extra])


def test_identity_token_mint_writes_file_never_prints(home, monkeypatch, capsys, tmp_path):
    jwt = make_jwt()
    calls = []
    fake_gcloud(monkeypatch, jwt, calls)
    out = tmp_path / "tok.jwt"
    rc = main(["identity-token", "mint", "--audience", "https://api.anthropic.com", "--out", str(out),
               "--service-account", "claude@p.iam.gserviceaccount.com"])
    cap = capsys.readouterr()
    assert rc == 0
    assert out.read_text().strip() == jwt
    assert stat.S_IMODE(out.stat().st_mode) == 0o600
    assert "email=claude@p.iam.gserviceaccount.com" in cap.out
    assert jwt not in cap.out + cap.err and "SECRET-SIGNATURE" not in cap.out + cap.err
    assert "--impersonate-service-account=claude@p.iam.gserviceaccount.com" in calls[0]


def test_identity_token_mint_failure_is_error(home, monkeypatch, capsys, tmp_path):
    monkeypatch.setattr("portunus.identity_token.shutil.which", lambda _: "/usr/bin/gcloud")
    monkeypatch.setattr("portunus.identity_token.subprocess.run",
                        lambda cmd, **kw: subprocess.CompletedProcess(cmd, 1, "", "ERROR: denied"))
    rc = main(["identity-token", "mint", "--audience", "a", "--out", str(tmp_path / "t")])
    assert rc == 1
    assert "denied" in capsys.readouterr().err
    assert not (tmp_path / "t").exists()


def test_set_list_show_env_remove(home, capsys):
    assert _set() == 0
    assert _set("ff", "--workspace-id", "wrkspc_w", "--note", "ff.events") == 0
    capsys.readouterr()
    assert main(["anthropic-federation", "list", "--json"]) == 0
    listed = json.loads(capsys.readouterr().out)
    assert [p["name"] for p in listed] == ["ff", "mathew"]

    assert main(["anthropic-federation", "env", "mathew"]) == 0
    lines = capsys.readouterr().out.splitlines()
    assert "unset ANTHROPIC_API_KEY" in lines and "unset CLAUDE_CODE_OAUTH_TOKEN" in lines
    assert "unset ANTHROPIC_WORKSPACE_ID" in lines
    assert "export ANTHROPIC_FEDERATION_RULE_ID=fdrl_r" in lines

    assert main(["anthropic-federation", "env", "ff", "--json"]) == 0
    env = json.loads(capsys.readouterr().out)
    assert env["set"]["ANTHROPIC_WORKSPACE_ID"] == "wrkspc_w"

    assert main(["anthropic-federation", "show", "mathew", "--json"]) == 0
    shown = json.loads(capsys.readouterr().out)
    assert shown["token_file"].endswith("anthropic-federation/mathew.jwt")

    assert main(["anthropic-federation", "remove", "ff"]) == 0
    assert main(["anthropic-federation", "show", "ff"]) == 1


def test_set_rejects_bad_ids(home, capsys):
    rc = main(["anthropic-federation", "set", "x", "--organization-id", "nope", "--rule-id", "fdrl_r"])
    assert rc == 1
    assert "organization_id" in capsys.readouterr().err


def test_refresh_then_status(home, monkeypatch, capsys):
    jwt = make_jwt()
    fake_gcloud(monkeypatch, jwt)
    _set()
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-ant-api03-LEAKME")
    monkeypatch.setenv("CLAUDE_CONFIG_DIR", str(home / "claude-empty"))
    capsys.readouterr()
    assert main(["anthropic-federation", "status", "--strict"]) == 1  # no token yet
    assert main(["anthropic-federation", "refresh", "mathew"]) == 0
    assert (home / "anthropic-federation" / "mathew.jwt").read_text().strip() == jwt
    assert main(["anthropic-federation", "status", "mathew", "--json", "--strict"]) == 0
    cap = capsys.readouterr()
    assert "ANTHROPIC_API_KEY" in cap.out
    assert "LEAKME" not in cap.out + cap.err
    assert jwt not in cap.out + cap.err


def test_status_flags_api_key_helper(home, monkeypatch, capsys):
    cfg = home / "claude"
    cfg.mkdir()
    (cfg / "settings.json").write_text(json.dumps({"apiKeyHelper": "/usr/bin/print-key"}))
    monkeypatch.setenv("CLAUDE_CONFIG_DIR", str(cfg))
    _set()
    capsys.readouterr()
    rc = main(["anthropic-federation", "status", "--json"])
    data = json.loads(capsys.readouterr().out)
    assert rc == 0
    assert data["settings_conflicts"] == [f"{cfg / 'settings.json'}: apiKeyHelper"]


def test_verify_reports_expiry_never_tokens(home, monkeypatch, capsys):
    jwt = make_jwt()
    fake_gcloud(monkeypatch, jwt)
    _set()
    seen = {}

    def transport(url, body, timeout):
        seen.update(body)
        return {"access_token": "sk-ant-oat01-NEVERSHOW", "token_type": "Bearer", "expires_in": 3600, "scope": "user:inference"}
    monkeypatch.setattr("portunus.anthropic_federation._default_transport", transport)
    capsys.readouterr()
    assert main(["anthropic-federation", "verify", "mathew"]) == 0
    cap = capsys.readouterr()
    assert "expires_in=3600" in cap.out
    assert seen["assertion"] == jwt
    for secret in ("sk-ant-oat01-NEVERSHOW", jwt, "SECRET-SIGNATURE"):
        assert secret not in cap.out + cap.err


def test_exec_runs_child_federated(home, monkeypatch, capsys, tmp_path):
    jwt = make_jwt()
    fake_gcloud(monkeypatch, jwt)
    _set()
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-ant-LEAK")
    report = tmp_path / "r.json"
    code = ("import json,os,sys;json.dump({k:v for k,v in os.environ.items() if k.startswith('ANTHROPIC')},"
            "open(sys.argv[1],'w'));sys.exit(3)")
    rc = main(["anthropic-federation", "exec", "mathew", "--", sys.executable, "-c", code, str(report)])
    assert rc == 3
    env = json.loads(report.read_text())
    assert "ANTHROPIC_API_KEY" not in env
    assert env["ANTHROPIC_FEDERATION_RULE_ID"] == "fdrl_r"
    assert env["ANTHROPIC_SERVICE_ACCOUNT_ID"] == "svac_s"
    cap = capsys.readouterr()
    assert jwt not in cap.out + cap.err


def test_exec_without_command_errors(home, monkeypatch, capsys):
    fake_gcloud(monkeypatch, make_jwt())
    _set()
    assert main(["anthropic-federation", "exec", "mathew"]) == 1
