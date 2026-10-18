"""envtargets tests (PANT-1146): target spec parsing, the Vercel and Convex
env writers, and the Vercel production redeploy.

Every HTTP call goes through an injected fake transport; values below are
obviously fake."""
import pytest

from portunus.envtargets import (
    ConvexEnvTarget,
    TargetError,
    TargetSpecError,
    TargetWriter,
    VercelEnvTarget,
    convex_deployment_url,
    parse_target,
    parse_targets,
)

DEPLOY_KEY = "prod:pleasant-finch-44|FAKE_DEPLOY_KEY_BODY"
VERCEL_TOKEN = "FAKE_VERCEL_TOKEN"
NEW_VALUE = "FAKE_NEW_SECRET_VALUE_0123"


class _Ref:
    def __init__(self, name, tags=None):
        self.name = name
        self.sm_name = name
        self.tags = tags or {}


class _Registry(dict):
    def get(self, name, default=None):
        return super().get(name, default)


class _Resolver:
    VALUES = {"convex-prod-deploy-key": DEPLOY_KEY, "vercel-token": VERCEL_TOKEN,
              "preview-key": "preview:team:proj|FAKE"}

    def __init__(self, registry):
        self.registry = registry
        self.templates = []

    def resolve_call(self, template, boundary):
        self.templates.append(template)
        return boundary(self.VALUES[template[len("{{secret:"):-2]])


def _registry():
    reg = _Registry()
    for name in ("convex-prod-deploy-key", "vercel-token"):
        reg[name] = _Ref(name)
    reg["preview-key"] = _Ref("preview-key", {"convex_url": "https://x-y-1.convex.cloud/"})
    return reg


class _Transport:
    def __init__(self, routes):
        self.routes = routes   # list of (method, url-substring, (status, body))
        self.calls = []

    def __call__(self, method, url, headers, body, timeout):
        self.calls.append({"method": method, "url": url, "headers": dict(headers), "body": body})
        for i, (m, frag, resp) in enumerate(self.routes):
            if m == method and frag in url:
                if isinstance(resp, list):
                    return resp.pop(0) if len(resp) > 1 else resp[0]
                return resp
        raise AssertionError(f"unexpected {method} {url}")


def _writer(routes, **kw):
    reg = _registry()
    transport = _Transport(routes)
    return TargetWriter(_Resolver(reg), registry=reg, transport=transport, sleep=lambda s: None, **kw), transport


# --- spec parsing ---------------------------------------------------------------

def test_parse_convex_and_vercel_specs():
    assert parse_target("convex:convex-prod-deploy-key:ZERNIO_API_KEY") == ConvexEnvTarget(
        "convex-prod-deploy-key", "ZERNIO_API_KEY")
    t = parse_target("vercel:vercel-token:social-engine@firefly-events:ZERNIO_WEBHOOK_SECRET")
    assert t == VercelEnvTarget("vercel-token", "social-engine", "ZERNIO_WEBHOOK_SECRET",
                                "firefly-events", ("production",))
    assert t.auto_redeploy
    p = parse_target("vercel:vercel-token:social-engine:X:preview+development")
    assert p.targets == ("preview", "development") and not p.auto_redeploy and p.team == ""


@pytest.mark.parametrize("spec", [
    "convex:only-key", "convex::VAR", "vercel:tok:proj", "vercel:tok:proj:VAR:staging",
    "gcp:thing:VAR", "vercel:tok:@team:VAR",
])
def test_malformed_specs_raise(spec):
    with pytest.raises(TargetSpecError):
        parse_target(spec)


def test_parse_targets_accepts_separated_string_and_list():
    s = "convex:convex-prod-deploy-key:A, vercel:vercel-token:p:B"
    assert len(parse_targets(s)) == 2
    # Space-separated: the form a `--tags k=v,...` value can hold.
    assert len(parse_targets("convex:k:A vercel:t:p:B;convex:k:C")) == 3
    assert len(parse_targets(["convex:k:A"])) == 1
    assert parse_targets("") == []


def test_label_round_trips():
    for spec in ("convex:k:VAR", "vercel:t:p@team_1:VAR:production+preview"):
        assert parse_target(parse_target(spec).label) == parse_target(spec)


@pytest.mark.parametrize("key,override,url", [
    ("prod:pleasant-finch-44|x", "", "https://pleasant-finch-44.convex.cloud"),
    ("dev:calm-gnat-491|x", "", "https://calm-gnat-491.convex.cloud"),
    ("calm-gnat-491|x", "", "https://calm-gnat-491.convex.cloud"),
    ("preview:team:proj|x", "", ""),
    ("no-pipe", "", ""),
    ("preview:team:proj|x", "https://a-b-1.convex.cloud/", "https://a-b-1.convex.cloud"),
])
def test_convex_deployment_url(key, override, url):
    assert convex_deployment_url(key, override) == url


# --- Convex ---------------------------------------------------------------------

def test_convex_preflight_and_write():
    w, t = _writer([
        ("GET", "/api/v1/list_environment_variables",
         (200, {"environmentVariables": {"ZERNIO_API_KEY": "FAKE_OLD"}})),
        ("POST", "/api/v1/update_environment_variables", (200, {})),
    ])
    target = ConvexEnvTarget("convex-prod-deploy-key", "ZERNIO_API_KEY")
    assert w.preflight(target) == "reachable; variable exists"
    w.write(target, NEW_VALUE)
    post = t.calls[-1]
    assert post["url"] == "https://pleasant-finch-44.convex.cloud/api/v1/update_environment_variables"
    assert post["headers"]["Authorization"] == f"Convex {DEPLOY_KEY}"
    assert post["body"] == {"changes": [{"name": "ZERNIO_API_KEY", "value": NEW_VALUE}]}


def test_convex_preflight_reports_missing_variable():
    w, _ = _writer([("GET", "list_environment_variables", (200, {"environmentVariables": {}}))])
    assert "will be created" in w.preflight(ConvexEnvTarget("convex-prod-deploy-key", "X"))


def test_convex_uses_convex_url_tag_for_preview_key():
    w, t = _writer([("GET", "list_environment_variables", (200, {"environmentVariables": {}}))])
    w.preflight(ConvexEnvTarget("preview-key", "X"))
    assert t.calls[0]["url"].startswith("https://x-y-1.convex.cloud/api/v1/")


def test_convex_errors_carry_no_value_or_body():
    w, _ = _writer([("POST", "update_environment_variables",
                     (403, {"code": "BadDeployKey", "message": f"echo {NEW_VALUE}"}))])
    with pytest.raises(TargetError) as exc:
        w.write(ConvexEnvTarget("convex-prod-deploy-key", "X"), NEW_VALUE)
    msg = str(exc.value)
    assert "403" in msg and "BadDeployKey" in msg
    assert NEW_VALUE not in msg and DEPLOY_KEY not in msg and "echo" not in msg


def test_convex_unknown_deploy_key_ref():
    w, _ = _writer([])
    with pytest.raises(TargetError, match="no deploy-key reference"):
        w.preflight(ConvexEnvTarget("missing", "X"))


# --- Vercel env -------------------------------------------------------------------

VT = VercelEnvTarget("vercel-token", "social-engine", "ZERNIO_WEBHOOK_SECRET", "firefly-events")


def test_vercel_existing_var_is_patched_in_place():
    w, t = _writer([
        ("GET", "/v10/projects/social-engine/env",
         (200, {"envs": [{"id": "env_1", "key": "ZERNIO_WEBHOOK_SECRET", "target": ["production"],
                          "value": "FAKE_OLD"}]})),
        ("PATCH", "/v9/projects/social-engine/env/env_1", (200, {"id": "env_1"})),
    ])
    assert "variable exists (targets: production)" in w.preflight(VT)
    w.write(VT, NEW_VALUE)
    patch = t.calls[-1]
    assert patch["body"] == {"value": NEW_VALUE}
    assert "slug=firefly-events" in patch["url"]
    assert patch["headers"]["Authorization"] == f"Bearer {VERCEL_TOKEN}"


def test_vercel_team_id_uses_teamId_param():
    w, t = _writer([("GET", "/env", (200, {"envs": []}))])
    w.preflight(VercelEnvTarget("vercel-token", "p", "X", "team_abc"))
    assert "teamId=team_abc" in t.calls[0]["url"]


def test_vercel_missing_var_is_created_with_upsert():
    w, t = _writer([
        ("GET", "/env", (200, {"envs": [{"id": "e", "key": "OTHER", "target": "production"}]})),
        ("POST", "/v10/projects/social-engine/env", (201, {"created": {"id": "e2"}, "failed": []})),
    ])
    w.write(VT, NEW_VALUE)
    post = t.calls[-1]
    assert "upsert=true" in post["url"]
    assert post["body"] == {"key": "ZERNIO_WEBHOOK_SECRET", "value": NEW_VALUE, "type": "encrypted",
                            "target": ["production"]}


def test_vercel_var_that_does_not_cover_all_targets_is_upserted():
    w, t = _writer([
        ("GET", "/env", (200, {"envs": [{"id": "e", "key": "X", "target": ["preview"]}]})),
        ("POST", "/env", (201, {"created": {}, "failed": []})),
    ])
    w.write(VercelEnvTarget("vercel-token", "p", "X", targets=("production", "preview")), NEW_VALUE)
    assert t.calls[-1]["method"] == "POST"


def test_vercel_201_with_failed_entry_is_an_error():
    w, _ = _writer([
        ("GET", "/env", (200, {"envs": []})),
        ("POST", "/env", (201, {"created": [], "failed": [{"error": {"code": "ENV_CONFLICT",
                                                                       "message": NEW_VALUE}}]})),
    ])
    with pytest.raises(TargetError) as exc:
        w.write(VT, NEW_VALUE)
    assert "ENV_CONFLICT" in str(exc.value) and NEW_VALUE not in str(exc.value)


def test_vercel_list_failure_is_reported():
    w, _ = _writer([("GET", "/env", (403, {"error": {"code": "forbidden"}}))])
    with pytest.raises(TargetError, match="HTTP 403 forbidden"):
        w.preflight(VT)


# --- Vercel redeploy ---------------------------------------------------------------

def _redeploy_routes(states):
    return [
        ("GET", "/v9/projects/social-engine", (200, {"id": "prj_1", "name": "social-engine"})),
        ("GET", "/v7/deployments", (200, {"deployments": [{"uid": "dpl_old"}]})),
        ("POST", "/v13/deployments", (200, {"id": "dpl_new"})),
        ("GET", "/v13/deployments/dpl_new", [(200, {"readyState": s}) for s in states]),
    ]


def test_redeploy_redeploys_current_production_and_waits_for_ready():
    w, t = _writer(_redeploy_routes(["QUEUED", "BUILDING", "READY"]))
    assert w.redeploy(VT) == "dpl_new"
    listing = next(c for c in t.calls if "/v7/deployments" in c["url"])
    assert "projectId=prj_1" in listing["url"] and "target=production" in listing["url"]
    post = next(c for c in t.calls if c["method"] == "POST")
    assert post["body"] == {"name": "social-engine", "deploymentId": "dpl_old", "target": "production"}
    assert "forceNew=1" in post["url"]
    assert sum(1 for c in t.calls if "/v13/deployments/dpl_new" in c["url"]) == 3


def test_redeploy_error_state_raises():
    w, _ = _writer(_redeploy_routes(["BUILDING", "ERROR"]))
    with pytest.raises(TargetError, match="ended ERROR"):
        w.redeploy(VT)


def test_redeploy_times_out():
    clock = iter(range(0, 10000, 100))
    reg = _registry()
    w = TargetWriter(_Resolver(reg), registry=reg, transport=_Transport(_redeploy_routes(["BUILDING"])),
                     sleep=lambda s: None, clock=lambda: next(clock), redeploy_timeout=250)
    with pytest.raises(TargetError, match="not READY"):
        w.redeploy(VT)


def test_redeploy_without_production_deployment_raises():
    w, _ = _writer([
        ("GET", "/v9/projects/social-engine", (200, {"id": "prj_1"})),
        ("GET", "/v7/deployments", (200, {"deployments": []})),
    ])
    with pytest.raises(TargetError, match="no READY production deployment"):
        w.redeploy(VT)
