"""`portunus rotation roll` tests (GH #230): rotate a Resend key, push it to
Convex / Vercel / GCP SM, verify, retire the old key -- or roll it all back.

Every HTTP call goes through one injected fake transport; GCP goes through a
fake backend. Tokens below are obviously fake.
"""
import ast
import inspect
import json
import textwrap

import pytest

from portunus.audit import AuditChain
from portunus.registry import Registry
from portunus import rollout
from portunus.rollout import RolloutError, build_plan, roll_everywhere
from portunus.rotation import ResendRotationAdapter, load_rotation_bindings

REF = "flayr-resend-api-key"
ADMIN = "re_FAKE_ADMIN"
OLD = "re_FAKE_OLD_TOKEN"
NEW = "re_FAKE_NEW_TOKEN"
OLD_ID, NEW_ID = "old-key-1111", "new-key-2222"
DEPLOY_KEY = "prod:pleasant-finch-44|FAKE_DEPLOY_KEY"
VERCEL_TOKEN = "FAKE_VERCEL_TOKEN"
HOOK_URL = "https://api.vercel.com/v1/integrations/deploy/prj_FAKE/hook123"
CONVEX = "https://pleasant-finch-44.convex.cloud"

AUTH = {"flayr-resend-admin": ADMIN, "flayr-convex-prod-deploy-key": DEPLOY_KEY,
        "ffe-vercel-token": VERCEL_TOKEN, "social-engine-deploy-hook": HOOK_URL}


class FakeResolver:
    """Hands each reference's value to the boundary only. The rolled ref
    itself is read from the local vault, like the real routed resolver."""

    def __init__(self, registry, local):
        self.registry = registry
        self._local = local
        self.names = []

    def resolve_call(self, template, boundary):
        name = template[len("{{secret:"):-2]
        self.names.append(name)
        value = self._local.access(name) if name == REF else AUTH[name]
        return boundary(value)


class World:
    """Fake Resend + Convex + Vercel + deploy hook behind one transport."""

    def __init__(self):
        self.calls = []
        self.convex_env = {"RESEND_API_KEY": OLD}
        self.vercel_env = {}
        self.resend_keys = {OLD_ID}
        self.fail = set()          # e.g. {"vercel-write"}
        self.function_value = {"status": "sent"}
        self.convex_lies = False   # list returns a stale value

    def __call__(self, method, url, headers, body, timeout):
        self.calls.append((method, url, dict(headers), body))
        if url.startswith("https://api.resend.com"):
            assert headers["Authorization"] in (f"Bearer {ADMIN}", f"Bearer {NEW}")
            if method == "POST":
                self.resend_keys.add(NEW_ID)
                return 200, {"id": NEW_ID, "token": NEW}
            if method == "GET":
                return 200, {"data": []}
            if method == "DELETE":
                if "resend-delete" in self.fail:
                    return 500, {"name": "internal_server_error"}
                self.resend_keys.discard(url.rsplit("/", 1)[1])
                return 200, {"deleted": True}
        if url.startswith(CONVEX):
            assert headers["Authorization"] == f"Convex {DEPLOY_KEY}"
            if url.endswith("/update_environment_variables"):
                if "convex-write" in self.fail:
                    return 500, {}
                for change in body["changes"]:
                    self.convex_env[change["name"]] = change["value"]
                return 200, {}
            if url.endswith("/list_environment_variables"):
                env = dict(self.convex_env)
                if self.convex_lies:
                    env["RESEND_API_KEY"] = "stale"
                return 200, {"environmentVariables": env}
            if url.endswith("/api/function"):
                if "function" in self.fail:
                    return 200, {"status": "error", "errorMessage": "Resend 401"}
                return 200, {"status": "success", "value": self.function_value}
        if url.startswith("https://api.vercel.com/v10/projects/"):
            assert headers["Authorization"] == f"Bearer {VERCEL_TOKEN}"
            if "vercel-write" in self.fail:
                return 403, {}
            self.vercel_env[body["key"]] = body["value"]
            return 200, {}
        if url == HOOK_URL:
            return 201, {"job": {"id": "j1"}}
        raise AssertionError(f"unexpected call {method} {url}")


class FakeGcp:
    def __init__(self, fail=False):
        self.versions = {}
        self.fail = fail

    def add_version(self, secret, value, project=""):
        from portunus.backend import BackendError
        if self.fail:
            raise BackendError("gcloud versions add failed: credential expired")
        self.versions.setdefault((project, secret), []).append(value)
        return "7"

    def access(self, secret, project=""):
        return self.versions[(project, secret)][-1]


TARGETS = ("convex:flayr-convex-prod-deploy-key:RESEND_API_KEY "
           "vercel:ffe-vercel-token:social-engine:RESEND_API_KEY "
           "gcp:ffe-cicd:social-engine-prod-resend-api-key")


@pytest.fixture
def env(home):
    from portunus.localvault import LocalEncryptedBackend
    registry = Registry()
    tags = {"resend_api_key_id": OLD_ID, "resend_admin_ref": "flayr-resend-admin",
            "rotation_targets": TARGETS, "vercel_team_id": "team_ffe",
            "vercel_deploy_hook_ref": "social-engine-deploy-hook",
            "rotation_verify": "convex-fn:flayr-convex-prod-deploy-key:emailSend:verifyResendKey",
            "rotation_verify_args": json.dumps({"to": "ops@example.invalid"})}
    registry.add(REF, REF, provider="resend", org="firefly-events", project="flayr", tags=tags,
                 injected_as={"RESEND_API_KEY": "env:RESEND_API_KEY"})
    for name in AUTH:
        registry.add(name, name, project="flayr-auth", tags={"n": name})
    local = LocalEncryptedBackend()
    local.store(REF, OLD)
    world, gcp = World(), FakeGcp()
    resolver = FakeResolver(registry, local)
    audit = AuditChain()
    adapter = ResendRotationAdapter(transport=world, local_backend=local, audit=audit)

    def plan(**kw):
        return build_plan(registry.get(REF), registry, resolver, transport=world,
                          gcp_backend_for=lambda project: gcp, **kw)

    def roll(**kw):
        p = kw.pop("plan", None) or plan()
        return roll_everywhere(registry.get(REF), adapter, p, resolver, local, audit=audit,
                               provider="resend", **kw)

    return dict(registry=registry, local=local, world=world, gcp=gcp, resolver=resolver,
                audit=audit, plan=plan, roll=roll)


# --- plan parsing -------------------------------------------------------------

def test_plan_reads_targets_hook_and_checks(env):
    p = env["plan"]()
    assert [t.spec for t in p.targets] == TARGETS.split()
    assert [h.name for h in p.hooks] == ["social-engine-deploy-hook"]
    assert [c.spec for c in p.checks] == [
        "convex-fn:flayr-convex-prod-deploy-key:emailSend:verifyResendKey"]


def test_plan_skip_target(env):
    p = env["plan"](skip=("gcp:ffe-cicd:social-engine-prod-resend-api-key",))
    assert len(p.targets) == 2


@pytest.mark.parametrize("spec, msg", [
    ("nope:x", "unknown target kind"),
    ("convex:missing-key:VAR", "no deploy-key reference"),
    ("vercel:ffe-vercel-token", "expected vercel:"),
    ("gcp:", "expected gcp:"),
])
def test_plan_rejects_bad_specs(env, spec, msg):
    env["registry"].retag(REF, tags={**env["registry"].get(REF).tags, "rotation_targets": spec})
    with pytest.raises(RolloutError, match=msg):
        env["plan"]()


def test_plan_var_defaults_to_single_injected_as(env):
    env["registry"].retag(REF, tags={**env["registry"].get(REF).tags,
                                     "rotation_targets": "convex:flayr-convex-prod-deploy-key"})
    assert env["plan"]().targets[0]._var == "RESEND_API_KEY"


# --- happy path ---------------------------------------------------------------

def test_roll_pushes_everywhere_verifies_and_deletes_old(env):
    w, gcp = env["world"], env["gcp"]
    result = env["roll"]()

    assert env["local"].access(REF) == NEW
    assert w.convex_env["RESEND_API_KEY"] == NEW
    assert w.vercel_env["RESEND_API_KEY"] == NEW
    assert gcp.versions[("ffe-cicd", "social-engine-prod-resend-api-key")] == [NEW]
    assert w.resend_keys == {NEW_ID}  # old deleted only after verify
    assert result.key_id == NEW_ID and result.retired_old
    assert result.written == TARGETS.split()
    assert "vercel:ffe-vercel-token:social-engine:RESEND_API_KEY" in result.unverifiable
    assert set(result.verified) == {
        "convex:flayr-convex-prod-deploy-key:RESEND_API_KEY",
        "gcp:ffe-cicd:social-engine-prod-resend-api-key",
        "convex-fn:flayr-convex-prod-deploy-key:emailSend:verifyResendKey"}
    assert env["registry"].get(REF).tags["resend_api_key_id"] == NEW_ID
    assert OLD_ID not in load_rotation_bindings()["resend"].superseded_key_ids


def test_roll_order_hook_after_writes_delete_last(env):
    w = env["world"]
    env["roll"]()
    urls = [u for _, u, _, _ in w.calls]
    hook = urls.index(HOOK_URL)
    vercel = next(i for i, u in enumerate(urls) if "/v10/projects/" in u)
    func = next(i for i, u in enumerate(urls) if u.endswith("/api/function"))
    delete = next(i for i, (m, _, _, _) in enumerate(w.calls) if m == "DELETE")
    assert vercel < hook < func < delete
    assert w.calls[delete][1].endswith(f"/api-keys/{OLD_ID}")


def test_vercel_upsert_shape(env):
    w = env["world"]
    env["roll"]()
    method, url, _, body = next(c for c in w.calls if "/v10/projects/" in c[1])
    assert url == "https://api.vercel.com/v10/projects/social-engine/env?upsert=true&teamId=team_ffe"
    assert {k: v for k, v in body.items() if k != "value"} == {
        "key": "RESEND_API_KEY", "type": "encrypted", "target": ["production"]}


def test_convex_function_call_shape(env):
    w = env["world"]
    env["roll"]()
    _, _, _, body = next(c for c in w.calls if c[1].endswith("/api/function"))
    assert body == {"path": "emailSend:verifyResendKey", "args": [{"to": "ops@example.invalid"}],
                    "format": "convex_encoded_json"}


def test_keep_old_leaves_old_key(env):
    result = env["roll"](keep_old=True)
    assert env["world"].resend_keys == {OLD_ID, NEW_ID}
    assert not result.retired_old
    assert OLD_ID in load_rotation_bindings()["resend"].superseded_key_ids


def test_refuses_upfront_without_old_key_id(env):
    tags = dict(env["registry"].get(REF).tags)
    del tags["resend_api_key_id"]
    env["registry"].retag(REF, tags=tags)
    with pytest.raises(RolloutError, match="--keep-old"):
        env["roll"]()
    assert env["world"].calls == []


def test_retire_failure_is_reported_not_rolled_back(env):
    env["world"].fail.add("resend-delete")
    result = env["roll"]()
    assert env["local"].access(REF) == NEW
    assert not result.retired_old and "refused" in result.retire_error


# --- rollback -----------------------------------------------------------------

def _assert_rolled_back(env):
    w, gcp = env["world"], env["gcp"]
    assert env["local"].access(REF) == OLD
    assert w.convex_env["RESEND_API_KEY"] == OLD
    assert w.resend_keys == {OLD_ID}  # new deleted, old untouched
    assert env["registry"].get(REF).tags["resend_api_key_id"] == OLD_ID
    b = load_rotation_bindings().get("resend")
    assert b is None or OLD_ID not in b.superseded_key_ids
    return w, gcp


@pytest.mark.parametrize("fail", ["vercel-write", "function"])
def test_failure_rolls_everything_back(env, fail):
    env["world"].fail.add(fail)
    with pytest.raises(RolloutError, match="Rolled back") as exc:
        env["roll"]()
    w, gcp = _assert_rolled_back(env)
    if fail == "function":
        # every target got the new value, then the old one back
        assert w.vercel_env["RESEND_API_KEY"] == OLD
        assert gcp.versions[("ffe-cicd", "social-engine-prod-resend-api-key")] == [NEW, OLD]
        assert [u for _, u, _, _ in w.calls].count(HOOK_URL) == 2
    assert "INCOMPLETE" not in str(exc.value)
    last = env["audit"].entries()[-1]
    assert (last["action"], last["result"]) == ("roll:resend", "rolled-back")


def test_readback_mismatch_rolls_back(env):
    env["world"].convex_lies = True
    with pytest.raises(RolloutError, match="does not hold the new value"):
        env["roll"]()
    _assert_rolled_back(env)


def test_unexpected_function_status_rolls_back(env):
    env["world"].function_value = {"status": "failed"}
    with pytest.raises(RolloutError, match="expected 'sent'"):
        env["roll"]()
    _assert_rolled_back(env)


def test_gcp_failure_rolls_back_and_skip_target_avoids_it(env):
    env["gcp"].fail = True
    with pytest.raises(RolloutError, match="credential expired"):
        env["roll"]()
    _assert_rolled_back(env)
    env["gcp"].fail = False
    p = env["plan"](skip=("gcp:ffe-cicd:social-engine-prod-resend-api-key",))
    assert env["roll"](plan=p).retired_old


def test_incomplete_rollback_is_named(env):
    w = env["world"]
    w.fail.add("function")
    orig = w.__call__

    # Vercel accepts the new value, then refuses the rollback write.
    seen = {"n": 0}

    def transport(method, url, headers, body, timeout):
        if "/v10/projects/" in url:
            seen["n"] += 1
            if seen["n"] > 1:
                return 403, {}
        return orig(method, url, headers, body, timeout)

    p = build_plan(env["registry"].get(REF), env["registry"], env["resolver"], transport=transport,
                   gcp_backend_for=lambda project: env["gcp"])
    with pytest.raises(RolloutError, match="ROLLBACK INCOMPLETE.*vercel:ffe-vercel-token") as exc:
        env["roll"](plan=p)
    assert OLD not in str(exc.value) and NEW not in str(exc.value)


# --- secret boundary ------------------------------------------------------------

def test_no_value_in_any_error_message(env):
    for fail in ("vercel-write", "function", "convex-write"):
        env["world"].fail = {fail}
        with pytest.raises(RolloutError) as exc:
            env["roll"]()
        for secret in (OLD, NEW, ADMIN, DEPLOY_KEY, VERCEL_TOKEN, HOOK_URL):
            assert secret not in str(exc.value)


def test_result_carries_no_value(env):
    result = env["roll"]()
    blob = json.dumps(result.__dict__)
    for secret in (OLD, NEW, ADMIN, DEPLOY_KEY, VERCEL_TOKEN):
        assert secret not in blob


def test_values_never_reach_print_log_or_audit():
    """Structural check: in rollout.py no name that can hold a value
    (value / old_value / new_value / key / token / url / current) is an
    argument to print, a logger, audit.append or an exception message."""
    tree = ast.parse(textwrap.dedent(inspect.getsource(rollout)))
    tainted = {"value", "old_value", "new_value", "key", "token", "url", "current"}
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call):
            continue
        func = node.func
        name = func.id if isinstance(func, ast.Name) else getattr(func, "attr", "")
        if name in ("print", "append", "info", "warning", "error", "debug",
                    "RolloutError", "RotationAdapterError"):
            for arg in list(node.args) + [k.value for k in node.keywords]:
                names = {n.id for n in ast.walk(arg) if isinstance(n, ast.Name)}
                assert not names & tainted, f"{names & tainted} flows into {name}()"


# --- CLI ------------------------------------------------------------------------

def test_cli_dry_run_prints_plan_and_touches_nothing(home, capsys):
    from portunus.cli import main
    reg = Registry()
    reg.add("deploy-key", "deploy-key", project="p", tags={"k": "1"})
    reg.add(REF, REF, provider="resend", tags={
        "resend_api_key_id": OLD_ID, "rotation_targets": "convex:deploy-key:RESEND_API_KEY"})
    assert main(["rotation", "roll", REF, "--dry-run"]) == 0
    out = capsys.readouterr().out
    assert "convex:deploy-key:RESEND_API_KEY" in out
    assert "delete the old key" in out


def test_cli_requires_yes_without_tty(home, capsys, monkeypatch):
    from portunus.cli import main
    reg = Registry()
    reg.add(REF, REF, provider="resend", tags={"resend_api_key_id": OLD_ID})
    monkeypatch.setattr("sys.stdin.isatty", lambda: False)
    assert main(["rotation", "roll", REF]) == 1
    assert "--yes" in capsys.readouterr().err


def test_cli_unsupported_provider(home, capsys):
    from portunus.cli import main
    Registry().add("gh", "gh", provider="github")
    assert main(["rotation", "roll", "gh", "--dry-run"]) == 1
    assert "no roll support" in capsys.readouterr().err


def test_cli_resolve_stdin_warns_it_prints_a_path(home, capsys, monkeypatch):
    """GH #230: piping `resolve --stdin` into `vercel env add` stored the
    temp-file path. The command now says so on stderr when stdout is a pipe;
    the file itself holds the value (plus the template's trailing newline)."""
    import io
    import os
    from portunus.cli import main
    Registry().add("x", "x")
    monkeypatch.setenv("PORTUNUS_BACKEND", "mock")
    monkeypatch.setenv("PORTUNUS_MOCK_X", "fake-value")
    monkeypatch.setattr("sys.stdin", io.StringIO("{{secret:x}}\n"))
    assert main(["resolve", "--stdin"]) == 0
    captured = capsys.readouterr()
    path = captured.out.strip()
    try:
        assert "temp-file PATH" in captured.err and "trailing newline" in captured.err
        assert "fake-value" not in captured.out + captured.err
        with open(path) as fh:
            assert fh.read() == "fake-value\n"
    finally:
        os.unlink(path)
