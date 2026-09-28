"""`portunus health` -- read-only deep self-check (PANT-854).

No test here resolves a secret value: the local-vault fixture stores one
through LocalEncryptedBackend.store() and then asserts the health output and
the audit log never contain it, and a structural test proves health.py has
no path to a backend's access()."""
import ast
import inspect
import json
import os
import subprocess

import pytest

from portunus import AuditChain, Registry
from portunus import health as health_mod
from portunus.backend import VaultBinding, save_vault_bindings
from portunus.cli import main
from portunus.localvault import LocalEncryptedBackend

SECRET = "sk-health-canary-3f9a1c"


@pytest.fixture
def healthy(home, monkeypatch):
    """A realistic, healthy home: references, a local vault holding a value,
    and a few audit entries."""
    monkeypatch.delenv("PORTUNUS_BACKEND", raising=False)
    monkeypatch.delenv("PORTUNUS_GCP_PROJECT", raising=False)
    os.chmod(home, 0o700)
    registry = Registry()
    registry.add("shared-anthropic", "dostal-shared-anthropic")
    LocalEncryptedBackend().store("dostal-shared-anthropic", SECRET)
    audit = AuditChain()
    audit.append("drop", "shared-anthropic", "ok")
    audit.append("state", "shared-anthropic", "ok:enabled")
    return home


def _check(result, name):
    return next(c for c in result["checks"] if c["name"] == name)


def _run_cli(capsys):
    rc = main(["health", "--json"])
    return rc, json.loads(capsys.readouterr().out)


def _snapshot(home):
    return {p.name: (p.stat().st_mode, p.read_bytes()) for p in home.iterdir() if p.is_file()}


# --- healthy ----------------------------------------------------------------
def test_healthy_home_is_ok_and_exits_0(healthy, capsys):
    rc, result = _run_cli(capsys)
    assert rc == 0
    assert result["status"] == "ok"
    assert set(result) == {"status", "checks"}
    names = {c["name"] for c in result["checks"]}
    assert {"home", "permissions", "registry", "audit_chain", "audit_clock",
            "backend:local"} <= names
    for check in result["checks"]:
        assert set(check) == {"name", "ok", "detail"}
        assert check["ok"] is True, check


def test_health_never_emits_or_audits_a_value(healthy, capsys):
    before = len(AuditChain().entries())
    main(["health", "--json"])
    main(["health"])
    out = capsys.readouterr()
    assert SECRET not in out.out and SECRET not in out.err
    assert SECRET not in (healthy / "audit.log").read_text()
    assert len(AuditChain().entries()) == before


def test_health_is_read_only(healthy, capsys):
    os.chmod(healthy, 0o755)  # would be "fixed" by paths.home()
    before = _snapshot(healthy)
    main(["health", "--json"])
    assert _snapshot(healthy) == before
    assert oct(healthy.stat().st_mode & 0o777) == oct(0o755)


def test_missing_home_is_down_and_is_not_created(tmp_path, monkeypatch, capsys):
    missing = tmp_path / "nope"
    monkeypatch.setenv("PORTUNUS_HOME", str(missing))
    rc, result = _run_cli(capsys)
    assert rc == 2
    assert result["status"] == "down"
    assert not missing.exists()


def test_empty_home_is_ok(home, monkeypatch, capsys):
    monkeypatch.delenv("PORTUNUS_BACKEND", raising=False)
    os.chmod(home, 0o700)
    rc, result = _run_cli(capsys)
    assert rc == 0, result
    assert _check(result, "registry")["detail"].startswith("no registry yet")


# --- down: corrupt registry ---------------------------------------------------
def test_corrupt_registry_is_down(healthy, capsys):
    (healthy / "registry.json").write_text("{not json")
    rc, result = _run_cli(capsys)
    assert rc == 2
    assert result["status"] == "down"
    assert _check(result, "registry")["ok"] is False


def test_registry_with_an_unknown_field_is_down(healthy, capsys):
    raw = json.loads((healthy / "registry.json").read_text())
    raw["shared-anthropic"]["bogus"] = 1
    (healthy / "registry.json").write_text(json.dumps(raw))
    rc, result = _run_cli(capsys)
    assert rc == 2
    assert "bogus" in _check(result, "registry")["detail"]


# --- degraded: audit chain / clock --------------------------------------------
def test_tampered_audit_entry_breaks_the_chain(healthy, capsys):
    log = healthy / "audit.log"
    lines = log.read_text().splitlines()
    entry = json.loads(lines[0])
    entry["actor"] = "mallory"
    lines[0] = json.dumps(entry, separators=(",", ":"))
    log.write_text("\n".join(lines) + "\n")
    rc, result = _run_cli(capsys)
    assert rc == 1
    assert result["status"] == "degraded"
    assert _check(result, "audit_chain")["ok"] is False


def test_garbage_audit_line_is_degraded(healthy, capsys):
    with (healthy / "audit.log").open("a") as fh:
        fh.write("not json\n")
    rc, result = _run_cli(capsys)
    assert rc == 1
    assert "unparseable" in _check(result, "audit_chain")["detail"]


def test_truncated_audit_tail_is_caught_by_the_clock(healthy, capsys):
    """Dropping the last line leaves the chain itself intact -- only the
    monotonic clock notices."""
    log = healthy / "audit.log"
    lines = log.read_text().splitlines()
    log.write_text(lines[0] + "\n")
    rc, result = _run_cli(capsys)
    assert rc == 1
    assert _check(result, "audit_chain")["ok"] is True
    clock = _check(result, "audit_clock")
    assert clock["ok"] is False and "missing" in clock["detail"]


def test_rolled_back_clock_is_degraded(healthy, capsys):
    (healthy / ".clock").write_text("1")
    rc, result = _run_cli(capsys)
    assert rc == 1
    assert "rolled back" in _check(result, "audit_clock")["detail"]


# --- degraded: permissions ------------------------------------------------------
def test_group_readable_home_is_degraded(healthy, capsys):
    os.chmod(healthy, 0o750)
    rc, result = _run_cli(capsys)
    assert rc == 1
    assert "0750" in _check(result, "permissions")["detail"]


def test_world_readable_master_key_is_degraded(healthy, capsys):
    os.chmod(healthy / "master.key", 0o644)
    rc, result = _run_cli(capsys)
    assert rc == 1
    assert "master.key is 0644" in _check(result, "permissions")["detail"]


def test_down_outranks_degraded(healthy, capsys):
    os.chmod(healthy, 0o755)
    (healthy / "registry.json").write_text("[]")
    rc, result = _run_cli(capsys)
    assert rc == 2
    assert result["status"] == "down"


# --- backends -------------------------------------------------------------------
def test_local_vault_without_master_key_is_degraded(healthy, capsys):
    (healthy / "master.key").unlink()
    rc, result = _run_cli(capsys)
    assert rc == 1
    assert "master.key is missing" in _check(result, "backend:local")["detail"]


class _Runner:
    def __init__(self, returncode=0, stdout='{"createTime": "2026-01-01T00:00:00Z"}'):
        self.calls = []
        self.returncode = returncode
        self.stdout = stdout

    def __call__(self, cmd, **kwargs):
        self.calls.append(cmd)
        return subprocess.CompletedProcess(cmd, self.returncode, self.stdout, "denied")


@pytest.fixture
def gcp_home(healthy, monkeypatch):
    monkeypatch.setattr(health_mod.shutil, "which", lambda _: "/usr/bin/gcloud")
    save_vault_bindings({"demo": VaultBinding("demo", account="me@example.com")})
    Registry().add("gcp-token", "gcp-token-sm", project="demo")
    return healthy


def test_gcp_probe_uses_versions_describe_not_access(gcp_home):
    runner = _Runner()
    result = health_mod.run_health(runner=runner)
    assert result["status"] == "ok", result
    assert len(runner.calls) == 1
    cmd = runner.calls[0]
    assert cmd[:2] == ["gcloud", "--account=me@example.com"]
    assert ["secrets", "versions", "describe", "latest"] == cmd[2:6]
    assert "access" not in cmd
    assert "--secret=gcp-token-sm" in cmd and "--project=demo" in cmd


def test_gcp_probe_failure_is_degraded(gcp_home):
    result = health_mod.run_health(runner=_Runner(returncode=1, stdout=""))
    assert result["status"] == "degraded"
    assert "denied" in _check(result, "backend:gcp")["detail"]


def test_gcp_without_gcloud_binary_is_degraded(gcp_home, monkeypatch):
    monkeypatch.setattr(health_mod.shutil, "which", lambda _: None)
    runner = _Runner()
    result = health_mod.run_health(runner=runner)
    assert _check(result, "backend:gcp")["ok"] is False
    assert runner.calls == []


def test_wif_bound_gcp_reference_is_not_probed(healthy, monkeypatch):
    """Minting a WIF token appends to the audit log, so it is skipped."""
    monkeypatch.setattr(health_mod.shutil, "which", lambda _: "/usr/bin/gcloud")
    save_vault_bindings({"demo": VaultBinding("demo", wif_audience="//iam/aud")})
    Registry().add("gcp-token", "gcp-token-sm", project="demo")
    runner = _Runner()
    result = health_mod.run_health(runner=runner)
    assert runner.calls == []
    gcp = _check(result, "backend:gcp")
    assert gcp["ok"] is True and "skipped" in gcp["detail"]


def test_stub_backend_is_degraded(healthy, capsys):
    Registry().add("aws-thing", "aws-thing-sm", backend="aws")
    rc, result = _run_cli(capsys)
    assert rc == 1
    assert "stub" in _check(result, "backend:aws")["detail"]


# --- structural -----------------------------------------------------------------
def test_health_module_has_no_value_path():
    tree = ast.parse(inspect.getsource(health_mod))
    calls = {n.func.attr for n in ast.walk(tree)
             if isinstance(n, ast.Call) and isinstance(n.func, ast.Attribute)}
    assert "access" not in calls
    assert "decrypt" not in calls
    names = {n.id for n in ast.walk(tree) if isinstance(n, ast.Name)}
    assert not names & {"Resolver", "LocalEncryptedBackend", "AuditChain", "Registry", "home"}


def test_text_output_lists_every_check(healthy, capsys):
    rc = main(["health"])
    out = capsys.readouterr().out
    assert rc == 0
    assert out.startswith("portunus health: OK")
    assert "audit_chain" in out
