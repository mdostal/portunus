"""`portunus rotate zernio` CLI tests (PANT-1146). Real registry + local
vault; the Zernio and Convex HTTP transports are faked."""
import json

import pytest

from portunus import Registry, envtargets
from portunus import zernio as zernio_mod
from portunus.cli import main
from portunus.localvault import LocalEncryptedBackend

OLD_KEY = "sk_FAKE_OLD_CLI_KEY_1111111111111111"
NEW_KEY = "sk_FAKE_NEW_CLI_KEY_2222222222222222"
DEPLOY_KEY = "prod:pleasant-finch-44|FAKE_CLI_DEPLOY_KEY"
KEY_REF = "social-engine-prod-zernio-api-key"


@pytest.fixture
def seeded(home, monkeypatch):
    reg = Registry()
    reg.add(KEY_REF, KEY_REF, provider="zernio", env="prod",
            tags={"zernio_api_key_id": "key_old",
                  "zernio_targets": "convex:convex-prod-deploy-key:ZERNIO_API_KEY"})
    reg.add("social-engine-dev-zernio-api-key", "dev-key", provider="zernio", env="dev")
    reg.add("convex-prod-deploy-key", "convex-prod-deploy-key", provider="convex")
    local = LocalEncryptedBackend()
    local.store(KEY_REF, OLD_KEY)
    local.store("convex-prod-deploy-key", DEPLOY_KEY)

    state = {"zernio": [], "convex": []}

    def zernio_transport(method, url, headers, body, timeout):
        state["zernio"].append((method, url))
        if url.endswith("/api-keys") and method == "GET":
            return 200, {"apiKeys": [{"id": "key_old", "name": "prod"}]}
        if url.endswith("/api-keys") and method == "POST":
            return 201, {"apiKey": {"id": "key_new", "key": NEW_KEY}}
        if url.endswith("/profiles"):
            return 200, {"profiles": []}
        raise AssertionError(url)

    def convex_transport(method, url, headers, body, timeout):
        state["convex"].append((method, url, body))
        if "list_environment_variables" in url:
            return 200, {"environmentVariables": {"ZERNIO_API_KEY": "x"}}
        return 200, {}

    monkeypatch.setattr(zernio_mod, "_default_transport", zernio_transport)
    monkeypatch.setattr(envtargets, "_default_transport", convex_transport)
    return state


def test_dry_run_prints_plan_and_changes_nothing(seeded, capsys):
    assert main(["rotate", "zernio", "--api-key", "--env", "prod", "--dry-run"]) == 0
    out = capsys.readouterr().out
    assert f"rotate zernio api-key {KEY_REF} -- plan" in out
    assert "convex:convex-prod-deploy-key:ZERNIO_API_KEY: reachable; variable exists" in out
    assert not any(m == "POST" for m, _ in seeded["zernio"])
    assert LocalEncryptedBackend().access(KEY_REF) == OLD_KEY
    for v in (OLD_KEY, NEW_KEY, DEPLOY_KEY):
        assert v not in out


def test_non_tty_without_yes_refuses(seeded, capsys):
    assert main(["rotate", "zernio", "--ref", KEY_REF]) == 1
    assert "--yes" in capsys.readouterr().err
    assert LocalEncryptedBackend().access(KEY_REF) == OLD_KEY


def test_yes_rotates_and_pushes(seeded, capsys):
    assert main(["rotate", "zernio", "--ref", KEY_REF, "--yes", "--json"]) == 0
    out = capsys.readouterr().out
    report = json.loads(out)  # --yes skips the plan, so only the report is printed
    assert report["key_id"] == "key_new" and report["dry_run"] is False
    assert LocalEncryptedBackend().access(KEY_REF) == NEW_KEY
    update = [c for c in seeded["convex"] if "update_environment_variables" in c[1]]
    assert update and update[0][2] == {"changes": [{"name": "ZERNIO_API_KEY", "value": NEW_KEY}]}
    assert NEW_KEY not in out and OLD_KEY not in out
    assert Registry().get(KEY_REF).tags["zernio_api_key_id"] == "key_new"


def test_selection_errors(seeded, capsys):
    assert main(["rotate", "zernio", "--api-key"]) == 1
    assert "found 2" in capsys.readouterr().err
    assert main(["rotate", "zernio", "--env", "prod"]) == 1
    assert "say what to rotate" in capsys.readouterr().err
    assert main(["rotate", "zernio", "--ref", "nope"]) == 1
    assert main(["rotate", "zernio", "--ref", KEY_REF, "--target", "bogus:x", "--dry-run"]) == 1
    assert "unknown target kind" in capsys.readouterr().err


def test_rollback_is_webhook_only(seeded, capsys):
    assert main(["rotate", "zernio", "--ref", KEY_REF, "--rollback"]) == 1
    assert "webhook secret only" in capsys.readouterr().err


def test_flow_error_is_reported_without_value(seeded, capsys, monkeypatch):
    def failing(method, url, headers, body, timeout):
        return 401, {"error": f"bad {OLD_KEY}", "code": "invalid_credentials"}

    monkeypatch.setattr(zernio_mod, "_default_transport", failing)
    assert main(["rotate", "zernio", "--ref", KEY_REF, "--dry-run"]) == 1
    err = capsys.readouterr().err
    assert "invalid_credentials" in err and OLD_KEY not in err
