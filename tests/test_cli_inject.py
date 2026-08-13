"""portunus inject --tags ... --target env|file (story 03): end-to-end CLI
dispatch to the env/file adapters, gated through the existing Broker, with
adapter_resolution audit entries and portunus verify passing on them."""
import json
import os
import stat

import pytest

from portunus.audit import AuditChain
from portunus.cli import main

SECRET = "s3kr3t-do-not-leak-0xCAFE"


@pytest.fixture(autouse=True)
def _mock_backend(monkeypatch):
    monkeypatch.setenv("PORTUNUS_BACKEND", "mock")
    monkeypatch.setenv("PORTUNUS_MOCK_SM_VERCEL_MDOSTAL", SECRET)


def _register(monkeypatch=None):
    from portunus import Registry
    reg = Registry()
    reg.add("vercel-mdostal", "sm-vercel-mdostal", provider="vercel", project="mdostal.com", env="prod")
    return reg


def test_inject_env_target_sets_var_and_never_prints_value(home, capsys, monkeypatch):
    _register()
    monkeypatch.delenv("PORTUNUS_INJECT_TEST", raising=False)

    rc = main(["inject", "--tags", "provider=vercel,project=mdostal.com",
               "--target", "env", "--var", "PORTUNUS_INJECT_TEST"])
    captured = capsys.readouterr()

    assert rc == 0
    assert os.environ["PORTUNUS_INJECT_TEST"] == SECRET
    assert SECRET not in captured.out
    assert SECRET not in captured.err


def test_inject_file_target_env_format(home, capsys):
    _register()
    target = home / "out.env"

    rc = main(["inject", "--tags", "provider=vercel,project=mdostal.com",
               "--target", "file", "--path", str(target), "--format", "env", "--key", "MY_SECRET"])
    captured = capsys.readouterr()

    assert rc == 0
    assert target.read_text() == f"MY_SECRET={SECRET}\n"
    assert stat.S_IMODE(os.stat(target).st_mode) == 0o600
    assert SECRET not in captured.out
    assert SECRET not in captured.err


def test_inject_writes_adapter_resolution_audit_entry_never_the_value(home):
    _register()
    main(["inject", "--tags", "provider=vercel,project=mdostal.com",
          "--target", "env", "--var", "PORTUNUS_INJECT_TEST2"])

    audit = AuditChain()
    entries = [e for e in audit.entries() if e["action"] == "adapter_resolution"]
    assert len(entries) == 1
    assert entries[0]["secret"] == "sm-vercel-mdostal"
    assert SECRET not in json.dumps(entries[0])
    assert audit.verify() is True


def test_inject_ambiguous_tags_fails_closed_and_audits_no_value(home, capsys):
    from portunus import Registry
    reg = Registry()
    reg.add("a", "sm-a", provider="vercel", project="mdostal.com", env="prod")
    reg.add("b", "sm-b", provider="vercel", project="mdostal.com", env="staging")

    rc = main(["inject", "--tags", "provider=vercel,project=mdostal.com",
               "--target", "env", "--var", "X"])
    err = capsys.readouterr().err

    assert rc != 0
    assert "a" in err and "b" in err
    audit = AuditChain()
    assert not [e for e in audit.entries() if e["action"] == "adapter_resolution"]


def test_inject_failure_path_never_leaks_value_on_bad_target(home, capsys):
    _register()
    # Empty --var is an adapter-level failure, not a resolution failure --
    # confirms the failure path (not just the happy path) never leaks.
    rc = main(["inject", "--tags", "provider=vercel,project=mdostal.com",
               "--target", "env", "--var", ""])
    captured = capsys.readouterr()

    assert rc != 0
    assert SECRET not in captured.out
    assert SECRET not in captured.err
    audit = AuditChain()
    error_entries = [e for e in audit.entries() if e["action"] == "adapter_resolution"]
    assert len(error_entries) == 1
    assert SECRET not in json.dumps(error_entries[0])
