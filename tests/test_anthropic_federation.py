"""anthropic_federation: profiles, sanitized env, exchange, exec. No secret ever surfaces."""
import io
import json
import stat
import sys
import time

import pytest

from portunus import anthropic_federation as af
from portunus import identity_token as it
import base64
import subprocess


def make_jwt(claims):
    def b64(obj):
        return base64.urlsafe_b64encode(json.dumps(obj).encode()).decode().rstrip("=")
    return f"{b64({'alg': 'RS256', 'kid': 'k1'})}.{b64(claims)}.SIG"


def good_claims():
    now = int(time.time())
    return {"iss": "https://accounts.google.com", "aud": "https://api.anthropic.com",
            "sub": "1234567890", "email": "claude@p.iam.gserviceaccount.com", "iat": now, "exp": now + 3600}


class FakeRunner:
    def __init__(self, stdout="", stderr="", rc=0):
        self.stdout, self.stderr, self.rc = stdout, stderr, rc

    def __call__(self, cmd, **kw):
        return subprocess.CompletedProcess(cmd, self.rc, self.stdout, self.stderr)

ORG = "11111111-2222-3333-4444-555555555555"


def _set(name="mathew", **kw):
    base = dict(organization_id=ORG, federation_rule_id="fdrl_abc", service_account_id="svac_abc",
                impersonate_service_account="claude@p.iam.gserviceaccount.com")
    base.update(kw)
    return af.upsert(name, **base)


def test_upsert_load_roundtrip_0600(home):
    _set()
    _set("ff-events", organization_id="99999999-2222-3333-4444-555555555555",
         federation_rule_id="fdrl_ff", service_account_id="", workspace_id="wrkspc_ff")
    profiles = af.load()
    assert sorted(profiles) == ["ff-events", "mathew"]
    assert profiles["ff-events"].workspace_id == "wrkspc_ff"
    path = home / "anthropic-federation.json"
    assert stat.S_IMODE(path.stat().st_mode) == 0o600
    assert json.loads(path.read_text())["profiles"]["mathew"]["federation_rule_id"] == "fdrl_abc"


def test_upsert_partial_update_keeps_other_fields(home):
    _set()
    af.upsert("mathew", workspace_id="wrkspc_new")
    p = af.get("mathew")
    assert p.workspace_id == "wrkspc_new" and p.service_account_id == "svac_abc"


@pytest.mark.parametrize("field,value", [
    ("organization_id", "not-a-uuid"), ("federation_rule_id", "rule"),
    ("service_account_id", "sa_x"), ("workspace_id", "ws"), ("source", "nope"),
])
def test_validation(home, field, value):
    with pytest.raises(af.FederationError):
        _set(**{field: value})


def test_new_profile_needs_ids(home):
    with pytest.raises(af.FederationError, match="organization_id"):
        af.upsert("x", federation_rule_id="fdrl_a")


def test_remove_and_unknown(home):
    _set()
    af.remove("mathew")
    with pytest.raises(af.FederationError):
        af.get("mathew")


def test_build_env_strips_outranking_credentials(home):
    p = _set(service_account_id="")
    base = {
        "PATH": "/bin", "ANTHROPIC_API_KEY": "sk-ant-api-LEAK", "CLAUDE_CODE_OAUTH_TOKEN": "",
        "ANTHROPIC_AUTH_TOKEN": "x", "ANTHROPIC_PROFILE": "other", "ANTHROPIC_IDENTITY_TOKEN": "jwt",
        "CLAUDE_CODE_USE_VERTEX": "1", "ANTHROPIC_SERVICE_ACCOUNT_ID": "svac_from_other_profile",
        "ANTHROPIC_WORKSPACE_ID": "wrkspc_other",
    }
    env = af.build_env(p, base)
    for name in af.CONFLICTING_ENV:
        assert name not in env
    assert "ANTHROPIC_SERVICE_ACCOUNT_ID" not in env
    assert "ANTHROPIC_WORKSPACE_ID" not in env
    assert env["PATH"] == "/bin"
    assert env["ANTHROPIC_FEDERATION_RULE_ID"] == "fdrl_abc"
    assert env["ANTHROPIC_ORGANIZATION_ID"] == ORG
    assert env["ANTHROPIC_IDENTITY_TOKEN_FILE"] == str(home / "anthropic-federation" / "mathew.jwt")


def test_conflicts_reports_names_including_empty():
    assert af.conflicts({"ANTHROPIC_API_KEY": "", "PATH": "/bin"}) == ["ANTHROPIC_API_KEY"]


def test_claude_settings_conflicts(tmp_path):
    s = tmp_path / "settings.json"
    s.write_text(json.dumps({"apiKeyHelper": "/bin/key", "env": {"ANTHROPIC_API_KEY": "sk-ant-LEAK"}}))
    found = af.claude_settings_conflicts([s, tmp_path / "missing.json"])
    assert found == [f"{s}: apiKeyHelper", f"{s}: env.ANTHROPIC_API_KEY"]
    assert not any("LEAK" in f for f in found)


def test_shell_lines_unset_then_export(home):
    lines = af.shell_lines(_set())
    assert "unset ANTHROPIC_API_KEY" in lines
    assert "unset ANTHROPIC_WORKSPACE_ID" in lines
    assert "export ANTHROPIC_FEDERATION_RULE_ID=fdrl_abc" in lines


def test_exchange_sends_rule_body_and_drops_access_token(home):
    p = _set(workspace_id="wrkspc_w")
    seen = {}

    def transport(url, body, timeout):
        seen.update(url=url, body=body)
        return {"access_token": "sk-ant-oat01-SECRET", "token_type": "Bearer", "expires_in": 600, "scope": "user:inference"}

    out = af.exchange(p, "the.jwt.value", transport)
    assert seen["url"] == "https://api.anthropic.com/v1/oauth/token"
    assert seen["body"] == {
        "grant_type": "urn:ietf:params:oauth:grant-type:jwt-bearer", "assertion": "the.jwt.value",
        "federation_rule_id": "fdrl_abc", "organization_id": ORG,
        "service_account_id": "svac_abc", "workspace_id": "wrkspc_w",
    }
    assert out == {"token_type": "Bearer", "expires_in": 600, "scope": "user:inference"}
    assert "sk-ant-oat01-SECRET" not in repr(out)


def test_exchange_without_access_token_fails(home):
    with pytest.raises(af.FederationError):
        af.exchange(_set(), "j", lambda u, b, t: {"error": "x"})


def _minter_for(jwt):
    return it.IdentityTokenMinter("https://api.anthropic.com", "gcloud", runner=FakeRunner(stdout=jwt))


def test_run_exec_child_sees_federated_env_and_token_file(home, monkeypatch, tmp_path):
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-ant-api-LEAK")
    monkeypatch.setenv("CLAUDE_CODE_OAUTH_TOKEN", "oauth-LEAK")
    p = _set()
    jwt = make_jwt(good_claims())
    report = tmp_path / "child.json"
    code = (
        "import json,os,sys;"
        "f=os.environ['ANTHROPIC_IDENTITY_TOKEN_FILE'];"
        "json.dump({'keys':sorted(k for k in os.environ if k.startswith(('ANTHROPIC','CLAUDE_CODE'))),"
        "'tok':open(f).read().strip(),'mode':oct(os.stat(f).st_mode&0o777)},open(sys.argv[1],'w'));"
        "sys.exit(7)"
    )
    log = io.StringIO()
    rc = af.run_exec(p, [sys.executable, "-c", code, str(report)], minter=_minter_for(jwt), log=log)
    assert rc == 7
    seen = json.loads(report.read_text())
    assert seen["tok"] == jwt
    assert seen["mode"] == "0o600"
    assert "ANTHROPIC_API_KEY" not in seen["keys"]
    assert "CLAUDE_CODE_OAUTH_TOKEN" not in seen["keys"]
    assert {"ANTHROPIC_FEDERATION_RULE_ID", "ANTHROPIC_ORGANIZATION_ID", "ANTHROPIC_IDENTITY_TOKEN_FILE",
            "ANTHROPIC_SERVICE_ACCOUNT_ID"} <= set(seen["keys"])
    assert jwt not in log.getvalue()


def test_run_exec_mint_failure_does_not_start_child(home):
    p = _set()
    started = []
    minter = it.IdentityTokenMinter("aud", "gcloud", runner=FakeRunner(stderr="denied", rc=1))
    with pytest.raises(af.FederationError, match="could not mint"):
        af.run_exec(p, ["true"], minter=minter, popen=lambda *a, **k: started.append(a))
    assert started == []


@pytest.mark.parametrize("body", ["[]", '"x"', '{"profiles": []}', '{"profiles": {"m": "oops"}}'])
def test_load_rejects_wrong_shape_with_federation_error(home, body):
    (home / "anthropic-federation.json").write_text(body)
    with pytest.raises(af.FederationError):
        af.load()
