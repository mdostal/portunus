"""Clerk secret-key liveness + Vercel env parity target (PANT-1148, GH #145).

The Clerk secret key only reaches the outbound Authorization header, and a
Vercel env var value is digested inside the Vercel token's resolver boundary:
no result, CLI output, audit line or status file carries either one, nor the
Vercel token."""
import json

import pytest

from portunus import liveness
from portunus.backend import MockBackend
from portunus.cli import main

SECRET = "sk_live_clerk-secret-do-not-leak-0xC1E4"
STALE = "sk_live_clerk-stale-do-not-leak-0xDEAD"
OTHER = "sk_live_other-var-do-not-leak-0xB0B0"
TOKEN = "vcp-vercel-token-do-not-leak-0xFACE"
URL = "https://api.clerk.com/v1/jwks"
PROJECT = "prj_JSD2sPXbFcJuYQZutwdbV8FGBMj7"
TEAM = "team_9LvKhV1a433uROYAAXl6lbhV"


class FakeTransport:
    def __init__(self, code=200):
        self.code = code
        self.calls = []

    def __call__(self, method, url, headers, timeout):
        self.calls.append((method, url, dict(headers)))
        return self.code


def _env(key, value, target=("production",), type_="encrypted", decrypted=True, **extra):
    return {"id": f"env_{key}_{'_'.join(target)}", "key": key, "value": value, "type": type_,
            "target": list(target), "decrypted": decrypted, **extra}


class FakeVercelEnv:
    """JSON transport standing in for `GET /v10/projects/<p>/env?decrypt=true`."""

    def __init__(self, envs=None, code=200):
        self.envs = list(envs if envs is not None else [
            _env("CLERK_SECRET_KEY", SECRET),
            _env("CLERK_SECRET_KEY", OTHER, target=("preview", "development")),
            _env("AUTUMN_SECRET_KEY", OTHER),
        ])
        self.code = code
        self.calls = []

    def __call__(self, method, url, headers, timeout):
        self.calls.append((method, url, dict(headers)))
        if self.code != 200:
            return self.code, b""
        return 200, json.dumps({"envs": self.envs, "pagination": None}).encode()


def _add(stack, name, value, provider="clerk", **kw):
    stack["registry"].add(name, f"sm-{name}", provider=provider, **kw)
    stack["backend"].set(f"sm-{name}", value)
    return stack["registry"].get(name)


def _no_leak(text):
    for secret in (SECRET, STALE, OTHER, TOKEN):
        assert secret not in text


# --- liveness ----------------------------------------------------------------------

def test_clerk_is_registered():
    assert "clerk" in liveness.liveness_providers()
    assert isinstance(liveness.liveness_adapter_for("clerk"), liveness.ClerkLivenessAdapter)


@pytest.mark.parametrize("code,expected", [(200, "live"), (401, "dead"), (403, "unknown"),
                                           (404, "unknown"), (429, "unknown"), (500, "unknown")])
def test_clerk_probe(stack, code, expected):
    ref = _add(stack, "social-engine-prod-clerk-secret-key", SECRET + "\n")
    transport = FakeTransport(code)
    result = liveness.check_reference(ref, stack["resolver"], liveness.ClerkLivenessAdapter(transport=transport))
    assert (result.status, result.http_status) == (expected, code)
    assert transport.calls == [("GET", URL, {"Authorization": f"Bearer {SECRET}"})]
    assert result.detail == f"GET {URL} -> {code}"
    _no_leak(json.dumps(result.to_dict()))


@pytest.mark.parametrize("name", ["social-engine-prod-clerk-publishable-key",
                                  "social-engine-prod-clerk-webhook-secret",
                                  "flayr-admin-clerk-ids"])
def test_clerk_non_secret_key_references_are_not_probed(stack, name):
    ref = _add(stack, name, SECRET)
    adapter = liveness.ClerkLivenessAdapter(transport=FakeTransport())
    assert not adapter.can_probe(ref)
    result = adapter.check(ref, stack["resolver"])
    assert result.status == "unknown" and result.detail.startswith("not probed:")
    assert adapter._transport.calls == []
    assert ref not in liveness.checkable_references(stack["registry"])


@pytest.mark.parametrize("value", ["pk_live_Y2xlcmsuZXhhbXBsZS5jb20k", "whsec_abc123", "not-a-key"])
def test_clerk_value_that_is_not_a_secret_key_is_not_sent(stack, value):
    # Misfiled under a `-secret-key` name: sending it would read as a false dead.
    ref = _add(stack, "social-engine-dev-clerk-secret-key", value)
    adapter = liveness.ClerkLivenessAdapter(transport=FakeTransport(401))
    result = adapter.check(ref, stack["resolver"])
    assert result.status == "unknown" and "not a Clerk secret key" in result.detail
    assert adapter._transport.calls == []
    assert value not in json.dumps(result.to_dict())


def test_clerk_provider_tag_routes_gcp_reference(stack):
    ref = _add(stack, "social-engine-prod-clerk-secret-key", SECRET, provider="gcp",
               tags={"liveness_provider": "clerk"})
    assert liveness.provider_for(ref) == "clerk"
    assert ref in liveness.checkable_references(stack["registry"])


# --- Vercel env parity target --------------------------------------------------------

@pytest.fixture
def vercel(stack):
    token_ref = _add(stack, "vercel-deploy-token", TOKEN, provider="vercel", tags={"vercel_team_id": TEAM})
    ref = _add(stack, "social-engine-prod-clerk-secret-key", SECRET,
               injected_as={"CLERK_SECRET_KEY": "env:CLERK_SECRET_KEY"})
    return {"token_ref": token_ref, "ref": ref, "env": FakeVercelEnv()}


def _vercel_parity(stack, vercel, var="CLERK_SECRET_KEY", target="production"):
    return liveness.parity_check(vercel["ref"].name, {
        "ref": liveness.reference_fetcher(stack["resolver"], vercel["ref"].name),
        "vercel": liveness.vercel_env_fetcher(stack["resolver"], vercel["token_ref"], PROJECT, var, target,
                                              transport=vercel["env"]),
    })


def test_vercel_env_in_sync_one_request(stack, vercel):
    result = _vercel_parity(stack, vercel)
    assert result.in_sync, result
    assert vercel["env"].calls == [(
        "GET", f"https://api.vercel.com/v10/projects/{PROJECT}/env?decrypt=true&teamId={TEAM}",
        {"Authorization": f"Bearer {TOKEN}"})]


def test_vercel_env_drift_without_leaking(stack, vercel):
    stack["backend"].set("sm-social-engine-prod-clerk-secret-key", STALE)
    result = _vercel_parity(stack, vercel)
    assert result.groups == (("ref",), ("vercel",))
    _no_leak(json.dumps(result.to_dict()))
    _no_leak(repr(result))


def test_vercel_env_target_selects_entry(stack, vercel):
    # preview/development hold OTHER, so comparing against them is drift.
    assert not _vercel_parity(stack, vercel, target="preview").in_sync
    vercel["env"].envs.append(_env("CLERK_SECRET_KEY", SECRET, target=("preview",), gitBranch="feature-x"))
    assert not _vercel_parity(stack, vercel, target="preview").in_sync  # branch override ignored


def test_vercel_env_custom_environment(stack, vercel):
    vercel["env"].envs = [_env("CLERK_SECRET_KEY", SECRET, target=(), customEnvironmentIds=["env_dev"])]
    assert _vercel_parity(stack, vercel, target="dev").in_sync
    assert "customEnvironmentSlug=dev" in vercel["env"].calls[0][1]
    # ... but a custom-environment entry is never read for a standard target.
    assert _vercel_parity(stack, vercel).errors == {"vercel": "VercelEnvVarMissing"}


def test_vercel_team_slug_and_no_team(stack, vercel):
    stack["registry"].retag("vercel-deploy-token", tags={"vercel_team_id": "firefly-events"})
    vercel["token_ref"] = stack["registry"].get("vercel-deploy-token")
    _vercel_parity(stack, vercel)
    assert vercel["env"].calls[-1][1].endswith("env?decrypt=true&slug=firefly-events")
    stack["registry"].retag("vercel-deploy-token", tags={})
    vercel["token_ref"] = stack["registry"].get("vercel-deploy-token")
    _vercel_parity(stack, vercel)
    assert vercel["env"].calls[-1][1].endswith("env?decrypt=true")


@pytest.mark.parametrize("envs,error", [
    ([], "VercelEnvVarMissing"),
    ([_env("CLERK_SECRET_KEY", "", type_="sensitive", decrypted=False)], "VercelEnvUnreadable"),
    ([_env("CLERK_SECRET_KEY", "ciphertext", decrypted=False)], "VercelEnvUnreadable"),
    ([_env("CLERK_SECRET_KEY", SECRET), _env("CLERK_SECRET_KEY", SECRET)], "VercelEnvError"),
])
def test_vercel_env_unreadable_is_an_error_not_drift(stack, vercel, envs, error):
    vercel["env"].envs = envs
    result = _vercel_parity(stack, vercel)
    assert not result.in_sync
    assert result.errors == {"vercel": error}


def test_vercel_env_plain_type_needs_no_decrypt_flag(stack, vercel):
    vercel["env"].envs = [_env("CLERK_SECRET_KEY", SECRET, type_="plain", decrypted=False)]
    assert _vercel_parity(stack, vercel).in_sync


@pytest.mark.parametrize("code", [401, 403, 404, 500])
def test_vercel_env_http_error_carries_status_only(stack, vercel, code):
    vercel["env"].code = code
    fetch = liveness.vercel_env_fetcher(stack["resolver"], vercel["token_ref"], PROJECT, "CLERK_SECRET_KEY",
                                        transport=vercel["env"])
    with pytest.raises(liveness.VercelEnvError) as exc:
        fetch(lambda v: b"")
    assert str(exc.value) == f"list project env -> {code}"


@pytest.mark.parametrize("body", [b"<html>" + SECRET.encode(), b'{"envs": "x"}', b"[]"])
def test_vercel_env_malformed_response(stack, vercel, body):
    fetch = liveness.vercel_env_fetcher(stack["resolver"], vercel["token_ref"], PROJECT, "CLERK_SECRET_KEY",
                                        transport=lambda *a: (200, body))
    with pytest.raises(liveness.VercelEnvError) as exc:
        fetch(lambda v: b"")
    _no_leak(str(exc.value))


def test_vercel_env_errors_never_carry_values(stack, vercel):
    vercel["env"].envs = [_env("CLERK_SECRET_KEY", SECRET, type_="sensitive")]
    fetch = liveness.vercel_env_fetcher(stack["resolver"], vercel["token_ref"], PROJECT, "CLERK_SECRET_KEY",
                                        transport=vercel["env"])
    with pytest.raises(liveness.VercelEnvUnreadable) as exc:
        fetch(lambda v: b"")
    _no_leak(str(exc.value))


# --- target specs --------------------------------------------------------------------

def _fetchers(stack, ref, specs):
    return liveness.parity_fetchers(ref, specs, stack["registry"], stack["broker"], stack["resolver"],
                                    {"gcp": stack["backend"]}.__getitem__)


def test_parity_fetchers_vercel_specs(stack, vercel):
    specs = ["ref", f"vercel:vercel-deploy-token:{PROJECT}",
             f"vercel:vercel-deploy-token:{PROJECT}:CLERK_SECRET_KEY:dev"]
    assert list(_fetchers(stack, vercel["ref"], specs)) == specs


@pytest.mark.parametrize("spec,msg", [
    ("vercel:nope:prj", "no Vercel token reference 'nope'"),
    ("vercel:vercel-deploy-token", "name the project"),
    ("vercel:vercel-deploy-token:prj:VAR:production:extra", "expected vercel:"),
])
def test_parity_fetchers_reject_bad_vercel_specs(stack, vercel, spec, msg):
    with pytest.raises(liveness.ParityTargetError, match=msg):
        _fetchers(stack, vercel["ref"], ["ref", spec])


def test_parity_fetchers_vercel_needs_var_without_single_injected_as(stack, vercel):
    ref = _add(stack, "plain-clerk-secret-key", SECRET)
    with pytest.raises(liveness.ParityTargetError, match="name the env var"):
        _fetchers(stack, ref, ["ref", f"vercel:vercel-deploy-token:{PROJECT}"])


# --- CLI ---------------------------------------------------------------------------------

@pytest.fixture
def cli_env(home, monkeypatch):
    from portunus import Registry, cli

    monkeypatch.setenv("PORTUNUS_BACKEND", "mock")
    monkeypatch.setenv("PORTUNUS_MOCK_SM_CLERK", SECRET)
    monkeypatch.setenv("PORTUNUS_MOCK_SM_VERCEL", TOKEN)
    gcp = MockBackend({"sm-clerk": SECRET})

    def backend(kind, audit):
        if kind != "gcp":
            raise KeyError(kind)
        return gcp

    monkeypatch.setattr(cli, "_parity_backend", backend)
    env = FakeVercelEnv()
    monkeypatch.setattr(liveness, "_default_vercel_env_transport", env)
    clerk = FakeTransport(401)
    monkeypatch.setattr(liveness, "_default_transport", clerk)
    reg = Registry()
    reg.add("vercel-deploy-token", "sm-vercel", provider="vercel", tags={"vercel_team_id": TEAM})
    reg.add("social-engine-prod-clerk-secret-key", "sm-clerk", provider="gcp",
            injected_as={"CLERK_SECRET_KEY": "env:CLERK_SECRET_KEY"},
            tags={"liveness_provider": "clerk",
                  "parity_targets": f"ref gcp vercel:vercel-deploy-token:{PROJECT}"})
    return {"gcp": gcp, "env": env, "clerk": clerk, "home": home}


def _all_output(capsys, home):
    out = capsys.readouterr()
    text = out.out + out.err + (home / "audit.log").read_text()
    if (home / "liveness-status.json").exists():
        text += (home / "liveness-status.json").read_text()
    return out, text


def test_cli_parity_vercel_drift_exit_two_and_no_leak(cli_env, capsys):
    assert main(["parity", "check", "social-engine-prod-clerk-secret-key", "--json"]) == 0
    cli_env["gcp"].set("sm-clerk", STALE)
    assert main(["parity", "check", "--all"]) == 2
    out, text = _all_output(capsys, cli_env["home"])
    assert "DRIFT" in out.out and "gcp" in out.out
    _no_leak(text)


def test_cli_liveness_check_dead_clerk_key(cli_env, capsys):
    assert main(["liveness", "check", "social-engine-prod-clerk-secret-key", "--json"]) == 2
    out, text = _all_output(capsys, cli_env["home"])
    assert '"dead"' in out.out
    assert cli_env["clerk"].calls == [("GET", URL, {"Authorization": f"Bearer {SECRET}"})]
    _no_leak(text)
