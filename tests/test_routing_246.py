"""#246: a value is resolved from where it was STORED, not from wherever the
project's binding points. Every write path (drop, drop-bulk, prompt CLI,
MCP prompt, MCP drop) must resolve the value it just stored under a
`gcp cached` binding while the remote answers NOT_FOUND -- and no path may
surface a bare GCP NOT_FOUND for a value that is sitting in the local vault.
"""
import json
import os
import subprocess
from types import SimpleNamespace

import pytest

from portunus import Registry
from portunus import prompt as prompt_mod
from portunus.backend import (
    BackendError, GcloudBackend, LocalFallbackBackend, SyncingBackend, VaultBinding,
    is_not_found, save_vault_bindings,
)
from portunus.cli import _build, main
from portunus.localvault import LocalEncryptedBackend
from portunus.registry import AmbiguousMatch

SECRET = "s3kr3t-246-0xBEEF"
NOT_FOUND = ("ERROR: (gcloud.secrets.versions.access) NOT_FOUND: Secret "
             "[projects/1/secrets/{name}] not found or has no versions.")


@pytest.fixture(autouse=True)
def _local_backend(monkeypatch):
    monkeypatch.delenv("PORTUNUS_BACKEND", raising=False)


class FakeGcloud:
    """Stands in for subprocess.run in portunus.backend. `remote` maps
    sm_name -> value; anything else answers NOT_FOUND."""

    def __init__(self, remote=None, fail=None):
        self.remote = dict(remote or {})
        self.fail = fail  # stderr for a non-NOT_FOUND failure (auth/network)
        self.calls = []

    def __call__(self, cmd, capture_output=True, text=True, timeout=None, input=None, **kw):
        self.calls.append(cmd)
        if self.fail:
            return SimpleNamespace(returncode=1, stdout="", stderr=self.fail)
        verb = cmd[cmd.index("secrets") + 1:]
        if verb[0] == "create":
            self.remote[verb[1]] = input
            return SimpleNamespace(returncode=0, stdout="", stderr="")
        name = verb[2] if verb[0] == "versions" and verb[1] in ("add",) else None
        if verb[:2] == ["versions", "add"]:
            name = verb[2]
            if name not in self.remote:
                return SimpleNamespace(returncode=1, stdout="",
                                       stderr=NOT_FOUND.format(name=name))
            self.remote[name] = input
            return SimpleNamespace(returncode=0, stdout="2", stderr="")
        # versions access latest <name> / versions describe latest --secret=<name>
        name = next((a.split("=", 1)[1] for a in cmd if a.startswith("--secret=")), None)
        if name is None:
            name = verb[3]
        if name not in self.remote:
            return SimpleNamespace(returncode=1, stdout="", stderr=NOT_FOUND.format(name=name))
        if verb[1] == "describe":
            return SimpleNamespace(returncode=0, stdout=json.dumps(
                {"name": "projects/1/secrets/x/versions/1", "createTime": "T1"}), stderr="")
        return SimpleNamespace(returncode=0, stdout=self.remote[name], stderr="")


@pytest.fixture
def gcp(monkeypatch, gcloud_on_path, home):
    fake = FakeGcloud()
    monkeypatch.setattr("portunus.backend.subprocess.run", fake)
    save_vault_bindings({"demo": VaultBinding("demo", backend="gcp", sync_mode="cached")})
    return fake


def _resolve(name):
    _, _, _, resolver = _build()
    seen = {}
    resolver.resolve_call("{{secret:%s}}" % name, lambda v: seen.setdefault("v", v))
    return seen["v"]


def _enable(name):
    assert main(["state", name, "enabled"]) == 0


# --- every write path resolves what it just stored ---------------------------

def test_drop_resolves_under_gcp_cached_binding(gcp, home, capsys):
    vf = home / "v.txt"
    vf.write_text(SECRET)
    assert main(["drop", "api", "demo-api", "--project", "demo", "--value-file", str(vf)]) == 0
    _enable("api")
    capsys.readouterr()
    assert Registry().get("api").backend == "local"
    assert _resolve("api") == SECRET


def test_drop_bulk_resolves_under_gcp_cached_binding(gcp, home, capsys):
    f = home / "bulk.json"
    f.write_text(json.dumps([{"name": "b1", "sm_name": "demo-b1", "value": SECRET,
                              "project": "demo"}]))
    assert main(["drop-bulk", str(f), "--json"]) == 0
    _enable("b1")
    capsys.readouterr()
    assert Registry().get("b1").backend == "local"
    assert _resolve("b1") == SECRET


def _answer(monkeypatch, value):
    def fake_run(argv, **kw):
        return subprocess.CompletedProcess(argv, 0, stdout=value + "\n", stderr="")
    monkeypatch.setattr(prompt_mod.subprocess, "run", fake_run)
    monkeypatch.setattr(prompt_mod, "available_surfaces", lambda **kw: ["kdialog"])


def test_prompt_cli_resolves_under_gcp_cached_binding(gcp, home, monkeypatch, capsys):
    _answer(monkeypatch, SECRET)
    assert main(["prompt", "pw", "demo-pw", "--project", "demo", "--enable"]) == 0
    capsys.readouterr()
    assert Registry().get("pw").backend == "local"
    assert _resolve("pw") == SECRET


def test_mcp_prompt_resolves_under_gcp_cached_binding(gcp, home, monkeypatch):
    from portunus import mcp_server

    _answer(monkeypatch, SECRET)
    result = mcp_server.portunus_prompt(name="pw", sm_name="demo-pw", project="demo", enable=True)
    assert result["stored"] is True
    assert Registry().get("pw").backend == "local"
    assert _resolve("pw") == SECRET


def test_mcp_drop_resolves_under_gcp_cached_binding(gcp, home):
    from portunus import mcp_server

    result = mcp_server.portunus_drop(name="m", sm_name="demo-m", value=SECRET, project="demo")
    assert "error" not in result
    _enable("m")
    assert Registry().get("m").backend == "local"
    assert _resolve("m") == SECRET


# --- legacy refs (backend="") fall back instead of surfacing NOT_FOUND --------

def test_legacy_unpinned_ref_falls_back_to_local_on_not_found(gcp, home, capsys):
    Registry().add("old", "demo-old", project="demo", state="enabled")
    LocalEncryptedBackend().store("demo-old", SECRET)
    assert _resolve("old") == SECRET
    err = capsys.readouterr().err
    assert "local" in err and SECRET not in err


def test_stale_cache_copy_is_not_served_when_remote_says_not_found(gcp, home):
    Registry().add("old", "demo-old", project="demo", state="enabled")
    local = LocalEncryptedBackend()
    local.store("demo:demo-old", "STALE-CACHED")
    local.store("demo-old", SECRET)
    assert _resolve("old") == SECRET


def test_auth_failure_with_local_copy_raises_a_hint_not_bare_error(home, monkeypatch, gcloud_on_path):
    monkeypatch.setattr("portunus.backend.subprocess.run",
                        FakeGcloud(fail="ERROR: Reauthentication required."))
    save_vault_bindings({"demo": VaultBinding("demo", backend="gcp")})
    Registry().add("old", "demo-old", project="demo", state="enabled")
    LocalEncryptedBackend().store("demo-old", SECRET)
    with pytest.raises(BackendError) as exc:
        _resolve("old")
    assert "local vault" in str(exc.value).lower() and "doctor --fix-routing" in str(exc.value)
    assert SECRET not in str(exc.value)


def test_not_found_with_no_local_copy_still_raises(gcp, home):
    Registry().add("ghost", "demo-ghost", project="demo", state="enabled")
    with pytest.raises(BackendError):
        _resolve("ghost")


def test_remote_value_wins_when_present(gcp, home):
    gcp.remote["demo-both"] = "FROM-GCP"
    Registry().add("both", "demo-both", project="demo", state="enabled")
    LocalEncryptedBackend().store("demo-both", "LOCAL")
    assert _resolve("both") == "FROM-GCP"


def test_is_not_found_markers():
    assert is_not_found(BackendError(NOT_FOUND.format(name="x")))
    assert is_not_found(BackendError("ResourceNotFoundException: nope"))
    assert not is_not_found(BackendError("PERMISSION_DENIED"))


def test_local_fallback_backend_unit(home):
    class Remote:
        attr = "passthrough"

        def access(self, sm_name, project=""):
            raise BackendError("NOT_FOUND")

    local = LocalEncryptedBackend()
    local.store("k", SECRET)
    fb = LocalFallbackBackend(Remote(), local, where="aws")
    assert fb.access("k") == SECRET
    assert fb.last_sync_result == "local-only"
    assert fb.attr == "passthrough"
    with pytest.raises(BackendError):
        fb.access("missing")


# --- collisions ----------------------------------------------------------------

def test_registry_add_rejects_identical_tags_when_asked(home):
    reg = Registry()
    reg.add("a", "sm-a", provider="stripe", project="demo", env="prod")
    with pytest.raises(AmbiguousMatch):
        reg.add("b", "sm-b", provider="stripe", project="demo", env="prod", reject_collisions=True)
    # opt-in only: discover/import paths are unaffected
    reg.add("c", "sm-c", provider="stripe", project="demo", env="prod")


def test_drop_rejects_identical_tags(home, capsys):
    vf = home / "v.txt"
    vf.write_text(SECRET)
    args = ["--provider", "stripe", "--project", "demo", "--value-file", str(vf)]
    assert main(["drop", "a", "sm-a", *args]) == 0
    assert main(["drop", "b", "sm-b", *args]) == 4
    assert Registry().get("b") is None
    assert SECRET not in capsys.readouterr().err


def test_mcp_drop_rejects_identical_tags(home):
    from portunus import mcp_server

    mcp_server.portunus_drop(name="a", sm_name="sm-a", value=SECRET, provider="stripe")
    result = mcp_server.portunus_drop(name="b", sm_name="sm-b", value=SECRET, provider="stripe")
    assert "error" in result


def test_prompt_rejects_identical_tags_before_asking(home, monkeypatch):
    from portunus import mcp_server

    Registry().add("a", "sm-a", provider="stripe", project="demo")
    asked = []
    monkeypatch.setattr(prompt_mod, "available_surfaces", lambda **kw: asked.append(1) or ["kdialog"])
    result = mcp_server.portunus_prompt(name="b", sm_name="sm-b", provider="stripe", project="demo")
    assert "error" in result
    assert not asked


# --- doctor --fix-routing ---------------------------------------------------------

def _stranded(name="old", sm="demo-old"):
    Registry().add(name, sm, project="demo", state="enabled")
    LocalEncryptedBackend().store(sm, SECRET)


def test_doctor_dry_run_changes_nothing(gcp, home, capsys):
    _stranded()
    assert main(["doctor", "--fix-routing", "--json"]) == 0
    data = json.loads(capsys.readouterr().out)
    assert data["dry_run"] is True
    assert [(m["name"], m["action"]) for m in data["mismatches"]] == [("old", "pin-local")]
    assert Registry().get("old").backend == ""


def test_doctor_apply_pins_absent_refs_local(gcp, home, capsys):
    _stranded()
    assert main(["doctor", "--fix-routing", "--apply"]) == 0
    assert Registry().get("old").backend == "local"
    capsys.readouterr()
    assert main(["doctor", "--fix-routing", "--json"]) == 0
    assert json.loads(capsys.readouterr().out)["mismatches"] == []


def test_doctor_skips_when_value_is_in_both_without_prefer(gcp, home, capsys):
    _stranded()
    gcp.remote["demo-old"] = "OTHER"
    assert main(["doctor", "--fix-routing", "--apply"]) == 1
    assert Registry().get("old").backend == ""
    assert main(["doctor", "--fix-routing", "--apply", "--prefer", "remote"]) == 0
    assert Registry().get("old").backend == "gcp"


def test_doctor_push_copies_to_gcp_and_drops_local(gcp, home, capsys):
    _stranded()
    assert main(["doctor", "--fix-routing", "--push", "--apply"]) == 0
    assert gcp.remote["demo-old"] == SECRET
    assert not LocalEncryptedBackend().has("demo-old")
    assert _resolve("old") == SECRET
    out = capsys.readouterr()
    assert SECRET not in out.out and SECRET not in out.err


def test_gcloud_push_creates_secret_on_not_found(home, monkeypatch, gcloud_on_path):
    fake = FakeGcloud()
    monkeypatch.setattr("portunus.backend.subprocess.run", fake)
    GcloudBackend(project="demo").push("new-secret", SECRET, project="demo")
    assert fake.remote["new-secret"] == SECRET
    assert all(SECRET not in " ".join(c) for c in fake.calls)  # never on argv


# --- health / vault status ------------------------------------------------------

def test_health_reports_routing_mismatch_as_degraded(gcp, home):
    from portunus.health import run_health

    _stranded()
    result = run_health(runner=gcp)
    routing = [c for c in result["checks"] if c["name"] == "routing"][0]
    assert routing["ok"] is False and "doctor --fix-routing" in routing["detail"]
    assert result["status"] in ("degraded", "down")


def test_health_routing_ok_after_pin(gcp, home):
    from portunus.health import run_health

    Registry().add("p", "demo-p", project="demo", backend="local")
    LocalEncryptedBackend().store("demo-p", SECRET)
    routing = [c for c in run_health(runner=gcp)["checks"] if c["name"] == "routing"][0]
    assert routing["ok"] is True


def test_vault_status_includes_routing(gcp, home, capsys):
    _stranded()
    assert main(["vault", "status", "--json"]) == 0
    data = json.loads(capsys.readouterr().out)
    assert data["routing"]["ok"] is False
    assert data["routing"]["mismatches"] == ["old"]


# --- bindings set guard ----------------------------------------------------------

def test_bindings_set_refuses_to_strand_local_values(home, capsys):
    _stranded()
    assert main(["bindings", "set", "demo", "--backend", "gcp", "--sync-mode", "cached"]) == 1
    err = capsys.readouterr().err
    assert "old" in err and "--pin-local" in err
    from portunus.backend import load_vault_bindings
    assert "demo" not in load_vault_bindings()


def test_bindings_set_pin_local_then_binds(home, capsys):
    _stranded()
    assert main(["bindings", "set", "demo", "--backend", "gcp", "--pin-local"]) == 0
    assert Registry().get("old").backend == "local"


def test_bindings_set_push_copies_then_binds(home, monkeypatch, gcloud_on_path, capsys):
    fake = FakeGcloud()
    monkeypatch.setattr("portunus.backend.subprocess.run", fake)
    _stranded()
    assert main(["bindings", "set", "demo", "--backend", "gcp", "--push"]) == 0
    assert fake.remote["demo-old"] == SECRET
    assert _resolve("old") == SECRET


def test_bindings_set_without_local_values_needs_no_flag(home):
    Registry().add("x", "demo-x", project="demo")  # no local value
    assert main(["bindings", "set", "demo", "--backend", "gcp"]) == 0


# --- retag --backend -------------------------------------------------------------

def test_retag_backend_pins_and_follow_clears(home, capsys):
    Registry().add("x", "sm-x", project="demo")
    assert main(["retag", "x", "--backend", "local"]) == 0
    assert Registry().get("x").backend == "local"
    assert main(["retag", "x", "--backend", "follow"]) == 0
    assert Registry().get("x").backend == ""
