"""Convex deploy-key liveness + cross-system parity (PANT-1151, GH #142).

The deploy key only reaches the outbound Authorization header, and a Convex
env var value is digested inside the deploy key's resolver boundary: no
result, CLI/MCP output, audit line or status file carries either one."""
import json

import pytest

from portunus import liveness
from portunus.audit import AuditChain
from portunus.backend import MockBackend
from portunus.cli import main

KEY_SECRET = "eyJ2-deploy-key-secret-part-0xCAFE"
DEPLOY_KEY = f"prod:happy-otter-123|{KEY_SECRET}"
VALUE = "am_sk_live_do-not-leak-0xBEEF"
STALE = "am_sk_stale_do-not-leak-0xDEAD"
URL = "https://happy-otter-123.convex.cloud"


class FakeTransport:
    def __init__(self, code=200):
        self.code = code
        self.calls = []

    def __call__(self, method, url, headers, timeout):
        self.calls.append((method, url, dict(headers)))
        return self.code


class FakeConvexEnv:
    """JSON transport standing in for `list_environment_variables`."""

    def __init__(self, env=None, code=200):
        self.env = dict(env or {})
        self.code = code
        self.calls = []

    def __call__(self, method, url, headers, timeout):
        self.calls.append((method, url, dict(headers)))
        if self.code != 200:
            return self.code, b""
        return 200, json.dumps({"environmentVariables": self.env}).encode()


def _add(stack, name, value, provider="convex", **kw):
    stack["registry"].add(name, f"sm-{name}", provider=provider, **kw)
    stack["backend"].set(f"sm-{name}", value)
    return stack["registry"].get(name)


def _no_leak(text):
    for secret in (KEY_SECRET, VALUE, STALE):
        assert secret not in text


# --- deployment URL ------------------------------------------------------------

@pytest.mark.parametrize("key,override,expected", [
    (DEPLOY_KEY, "", URL),
    ("dev:joyful-jaguar-123|eyJ...", "", "https://joyful-jaguar-123.convex.cloud"),
    ("bold-hyena-681|01c2", "", "https://bold-hyena-681.convex.cloud"),          # admin key
    ("preview:team:project|eyJ...", "", ""),                                    # names no deployment
    ("project:team:project|eyJ...", "", ""),
    ("preview:team:project|eyJ...", "https://x.convex.cloud/", "https://x.convex.cloud"),
    ("no-separator-at-all", "", ""),
    ("prod:evil.example.com/x|eyJ", "", ""),                                     # not a deployment name
    ("prod:|eyJ", "", ""),
])
def test_convex_deployment_url(key, override, expected):
    assert liveness.convex_deployment_url(key, override) == expected


# --- liveness ----------------------------------------------------------------------

def test_convex_is_registered():
    assert "convex" in liveness.liveness_providers()
    assert isinstance(liveness.liveness_adapter_for("convex"), liveness.ConvexLivenessAdapter)


@pytest.mark.parametrize("code,expected", [(200, "live"), (401, "dead"), (403, "dead"),
                                           (429, "unknown"), (500, "unknown")])
def test_convex_probe(stack, code, expected):
    ref = _add(stack, "social-engine-prod-convex-deploy-key", DEPLOY_KEY)
    transport = FakeTransport(code)
    result = liveness.check_reference(ref, stack["resolver"],
                                      liveness.ConvexLivenessAdapter(transport=transport))
    assert result.status == expected
    assert result.http_status == code
    assert transport.calls == [("GET", URL + "/api/check_admin_key",
                                {"Authorization": f"Convex {DEPLOY_KEY}"})]
    assert result.detail == f"GET {URL}/api/check_admin_key -> {code}"
    _no_leak(json.dumps(result.to_dict()))


def test_convex_preview_key_without_url_tag_is_not_probed(stack):
    ref = _add(stack, "convex-preview-deploy-key", f"preview:team:proj|{KEY_SECRET}")
    transport = FakeTransport()
    result = liveness.ConvexLivenessAdapter(transport=transport).check(ref, stack["resolver"])
    assert result.status == "unknown"
    assert "convex_url" in result.detail
    assert transport.calls == []
    _no_leak(json.dumps(result.to_dict()))


def test_convex_url_tag_overrides_derived_url(stack):
    ref = _add(stack, "convex-preview-deploy-key", f"preview:team:proj|{KEY_SECRET}",
               tags={"convex_url": "https://eu-thing-1.eu-west-1.convex.cloud"})
    transport = FakeTransport()
    result = liveness.ConvexLivenessAdapter(transport=transport).check(ref, stack["resolver"])
    assert result.status == "live"
    assert transport.calls[0][1] == "https://eu-thing-1.eu-west-1.convex.cloud/api/check_admin_key"


def test_convex_non_key_reference_is_skipped_and_not_probed(stack):
    ref = _add(stack, "social-engine-prod-convex-url", URL)
    adapter = liveness.ConvexLivenessAdapter(transport=FakeTransport())
    assert not adapter.can_probe(ref)
    assert adapter.check(ref, stack["resolver"]).status == "unknown"
    assert adapter._transport.calls == []
    assert ref not in liveness.checkable_references(stack["registry"])


# --- parity primitives ------------------------------------------------------------

def test_digesting_fetcher_must_return_a_digest():
    bad = liveness.digesting(lambda digest: "not-a-digest")
    result = liveness.parity_check("k", {"a": bad, "b": lambda: VALUE})
    assert result.errors == {"a": "TypeError"}


def test_reference_fetcher_digests_inside_the_boundary(stack):
    _add(stack, "autumn-secret-key", VALUE, provider="autumn")
    result = liveness.parity_check("autumn-secret-key", {
        "ref": liveness.reference_fetcher(stack["resolver"], "autumn-secret-key"),
        "other": lambda: VALUE,
    })
    assert result.in_sync


def test_reference_fetcher_enforces_policy(stack):
    _add(stack, "autumn-secret-key", VALUE, provider="autumn", state="dropped")
    result = liveness.parity_check("autumn-secret-key", {
        "ref": liveness.reference_fetcher(stack["resolver"], "autumn-secret-key"),
        "other": lambda: VALUE,
    })
    assert result.errors == {"ref": "NotInjectable"}


def test_policy_backend_fetcher_refuses_gated_reference(stack):
    ref = _add(stack, "autumn-secret-key", VALUE, provider="autumn", approval="required")

    class Exploding(MockBackend):
        def access(self, sm_name, project=""):
            raise AssertionError("backend read before the policy check")

    fetch = liveness.policy_backend_fetcher(stack["broker"], Exploding(), ref)
    result = liveness.parity_check(ref.name, {"gcp": fetch, "other": lambda: VALUE})
    assert result.errors == {"gcp": "ApprovalRequired"}


def test_policy_backend_fetcher_uses_reference_project(stack):
    ref = _add(stack, "autumn-secret-key", VALUE, provider="autumn", project="ffe-cicd")
    seen = []

    class Recording(MockBackend):
        def access(self, sm_name, project=""):
            seen.append((sm_name, project))
            return VALUE

    fetch = liveness.policy_backend_fetcher(stack["broker"], Recording(), ref)
    assert liveness.parity_check(ref.name, {"gcp": fetch, "x": lambda: VALUE}).in_sync
    assert seen == [("sm-autumn-secret-key", "ffe-cicd")]


@pytest.fixture
def convex(stack):
    key_ref = _add(stack, "social-engine-prod-convex-deploy-key", DEPLOY_KEY)
    ref = _add(stack, "social-engine-prod-autumn-secret-key", VALUE, provider="autumn",
               injected_as={"AUTUMN_SECRET_KEY": "env:AUTUMN_SECRET_KEY"})
    env = FakeConvexEnv({"AUTUMN_SECRET_KEY": VALUE, "OTHER": "unrelated"})
    return {"key_ref": key_ref, "ref": ref, "env": env}


def _convex_parity(stack, convex, var="AUTUMN_SECRET_KEY"):
    return liveness.parity_check(convex["ref"].name, {
        "ref": liveness.reference_fetcher(stack["resolver"], convex["ref"].name),
        "convex": liveness.convex_env_fetcher(stack["resolver"], convex["key_ref"], var,
                                              transport=convex["env"]),
    })


def test_convex_env_fetcher_in_sync(stack, convex):
    result = _convex_parity(stack, convex)
    assert result.in_sync
    assert convex["env"].calls == [("GET", URL + "/api/v1/list_environment_variables",
                                    {"Authorization": f"Convex {DEPLOY_KEY}"})]


def test_convex_env_fetcher_detects_drift_without_leaking(stack, convex):
    # The 2026-09-13 shape: the vault/GCP copy is stale, Convex has the live key.
    stack["backend"].set("sm-social-engine-prod-autumn-secret-key", STALE)
    result = _convex_parity(stack, convex)
    assert not result.in_sync
    assert result.groups == (("convex",), ("ref",))
    _no_leak(json.dumps(result.to_dict()))
    _no_leak(repr(result))


def test_convex_env_fetcher_missing_var(stack, convex):
    result = _convex_parity(stack, convex, var="NOT_THERE")
    assert result.errors == {"convex": "ConvexEnvVarMissing"}


@pytest.mark.parametrize("code", [401, 403, 500])
def test_convex_env_fetcher_http_error(stack, convex, code):
    convex["env"].code = code
    result = _convex_parity(stack, convex)
    assert result.errors == {"convex": "ConvexEnvError"}


def test_convex_env_fetcher_error_message_carries_no_value(stack, convex):
    convex["env"].code = 403
    fetch = liveness.convex_env_fetcher(stack["resolver"], convex["key_ref"], "AUTUMN_SECRET_KEY",
                                        transport=convex["env"])
    with pytest.raises(liveness.ConvexEnvError) as exc:
        fetch(lambda v: b"")
    assert str(exc.value) == "list_environment_variables -> 403"
    _no_leak(str(exc.value))


def test_convex_env_fetcher_malformed_response(stack, convex):
    def garbage(method, url, headers, timeout):
        return 200, b"<html>" + VALUE.encode()

    fetch = liveness.convex_env_fetcher(stack["resolver"], convex["key_ref"], "X", transport=garbage)
    with pytest.raises(liveness.ConvexEnvError) as exc:
        fetch(lambda v: b"")
    _no_leak(str(exc.value))


# --- target specs ------------------------------------------------------------------

def _fetchers(stack, ref, specs):
    return liveness.parity_fetchers(ref, specs, stack["registry"], stack["broker"], stack["resolver"],
                                    {"gcp": stack["backend"]}.__getitem__)


def test_parity_fetchers_specs(stack, convex):
    fetchers = _fetchers(stack, convex["ref"],
                         ["ref", "gcp:ffe-cicd", "convex:social-engine-prod-convex-deploy-key"])
    assert list(fetchers) == ["ref", "gcp:ffe-cicd", "convex:social-engine-prod-convex-deploy-key"]


@pytest.mark.parametrize("specs,msg", [
    (["ref", "s3"], "unknown target kind 's3'"),
    (["ref", "convex:nope"], "no deploy-key reference 'nope'"),
    (["ref"], "at least two targets"),
])
def test_parity_fetchers_reject_bad_specs(stack, convex, specs, msg):
    with pytest.raises(liveness.ParityTargetError, match=msg):
        _fetchers(stack, convex["ref"], specs)


def test_parity_fetchers_need_var_without_single_injected_as(stack, convex):
    ref = _add(stack, "plain", VALUE, provider="autumn")
    with pytest.raises(liveness.ParityTargetError, match="name the env var"):
        _fetchers(stack, ref, ["ref", "convex:social-engine-prod-convex-deploy-key"])


def test_parity_references_use_tag(stack):
    _add(stack, "tagged", VALUE, tags={"parity_targets": " ref  gcp;local,"})
    _add(stack, "dropped", VALUE, tags={"parity_targets": "ref,gcp"}, state="dropped")
    _add(stack, "untagged", VALUE)
    assert [r.name for r in liveness.parity_references(stack["registry"])] == ["tagged"]
    assert liveness.parity_specs(stack["registry"].get("tagged")) == ["ref", "gcp", "local"]


def test_parity_tag_round_trips_through_cli_tags(home):
    from portunus.cli import _parse_tags
    tags = _parse_tags("liveness_provider=autumn,parity_targets=gcp convex:k:AUTUMN_SECRET_KEY")
    ref = type("R", (), {"tags": tags})()
    assert liveness.parity_specs(ref) == ["gcp", "convex:k:AUTUMN_SECRET_KEY"]


# --- CLI + MCP ---------------------------------------------------------------------

@pytest.fixture
def cli_env(home, monkeypatch):
    """Mock-backend CLI: `ref` reads PORTUNUS_MOCK_*, `gcp` is a separate mock
    backend, Convex is FakeConvexEnv."""
    from portunus import Registry, cli

    monkeypatch.setenv("PORTUNUS_BACKEND", "mock")
    monkeypatch.setenv("PORTUNUS_MOCK_SM_AUTUMN", VALUE)
    monkeypatch.setenv("PORTUNUS_MOCK_SM_CONVEX_KEY", DEPLOY_KEY)
    gcp = MockBackend({"sm-autumn": VALUE})

    def backend(kind, audit):
        if kind != "gcp":
            raise KeyError(kind)
        return gcp

    monkeypatch.setattr(cli, "_parity_backend", backend)
    env = FakeConvexEnv({"AUTUMN_SECRET_KEY": VALUE})
    monkeypatch.setattr(liveness, "_default_json_transport", env)
    reg = Registry()
    reg.add("convex-deploy-key", "sm-convex-key", provider="convex")
    reg.add("autumn", "sm-autumn", provider="autumn",
            injected_as={"AUTUMN_SECRET_KEY": "env:AUTUMN_SECRET_KEY"},
            tags={"parity_targets": "ref gcp convex:convex-deploy-key"})
    return {"gcp": gcp, "env": env, "home": home}


def _all_output(capsys, home):
    out = capsys.readouterr()
    text = out.out + out.err + (home / "audit.log").read_text()
    for extra in ("liveness-status.json",):
        if (home / extra).exists():
            text += (home / extra).read_text()
    return out, text


def test_cli_parity_in_sync(cli_env, capsys):
    assert main(["parity", "check", "autumn", "--json"]) == 0
    out, text = _all_output(capsys, cli_env["home"])
    result = json.loads(out.out)["results"][0]
    assert result["in_sync"] is True
    assert result["groups"] == [["ref", "gcp", "convex:convex-deploy-key"]]
    _no_leak(text)
    entries = [e for e in AuditChain().entries() if e["action"] == "parity"]
    assert [(e["secret"], e["result"]) for e in entries] == [("sm-autumn", "in_sync")]


def test_cli_parity_drift_exit_two_and_no_leak(cli_env, capsys):
    cli_env["gcp"].set("sm-autumn", STALE)
    assert main(["parity", "check", "--all"]) == 2
    out, text = _all_output(capsys, cli_env["home"])
    assert "DRIFT" in out.out and "gcp" in out.out
    _no_leak(text)


def test_cli_parity_unreadable_exit_one(cli_env, capsys):
    cli_env["env"].code = 403
    assert main(["parity", "check", "autumn"]) == 1
    out, text = _all_output(capsys, cli_env["home"])
    assert "unreadable: convex:convex-deploy-key (ConvexEnvError)" in out.out
    _no_leak(text)


def test_cli_parity_target_flag_overrides_tag(cli_env, capsys):
    assert main(["parity", "check", "autumn", "--target", "ref", "--target", "gcp", "--json"]) == 0
    assert json.loads(capsys.readouterr().out)["results"][0]["groups"] == [["ref", "gcp"]]
    assert cli_env["env"].calls == []


def test_cli_parity_bad_spec_is_a_per_reference_error(cli_env, capsys):
    assert main(["parity", "check", "autumn", "--target", "ref", "--target", "s3", "--json"]) == 1
    result = json.loads(capsys.readouterr().out)["results"][0]
    assert result["in_sync"] is False
    assert "unknown target kind 's3'" in result["errors"]["targets"]


def test_cli_parity_requires_names_or_all(cli_env, capsys):
    assert main(["parity", "check"]) == 1
    assert main(["parity", "check", "nope"]) == 1


def test_cli_liveness_check_convex_key(cli_env, capsys, monkeypatch):
    transport = FakeTransport(403)
    monkeypatch.setattr(liveness, "_default_transport", transport)
    assert main(["liveness", "check", "convex-deploy-key"]) == 2
    out, text = _all_output(capsys, cli_env["home"])
    assert "DEAD" in out.out
    assert transport.calls[0][1] == URL + "/api/check_admin_key"
    _no_leak(text)


def test_mcp_parity_tool(cli_env):
    from portunus import mcp_server
    names = {t.name for t in mcp_server.mcp._tool_manager.list_tools()}
    assert "portunus_parity_check" in names
    cli_env["gcp"].set("sm-autumn", STALE)
    result = mcp_server.portunus_parity_check(names=["autumn"])
    assert result["results"][0]["groups"] == [["ref", "convex:convex-deploy-key"], ["gcp"]]
    _no_leak(json.dumps(result))
    assert mcp_server.portunus_parity_check(names=["nope"]) == {"error": "no reference 'nope' in registry"}
