"""PANT-1410: GCP auth without an interactively-maintained `gcloud auth login`.

- VaultBinding.credential_file hands gcloud a service account key /
  external_account config via CLOUDSDK_AUTH_CREDENTIAL_FILE_OVERRIDE.
- A reauth-required gcloud failure becomes GcpReauthRequired whose message
  carries self-service, non-interactive fixes (surfaced by `portunus health`).
"""
import json
import os
import subprocess
from types import SimpleNamespace

import pytest

from portunus import AuditChain, Registry
from portunus import health as health_mod
from portunus.backend import (
    BackendError,
    GcloudBackend,
    GcpReauthRequired,
    VaultBinding,
    gcp_reauth_remediation,
    is_gcloud_reauth_error,
    load_vault_bindings,
    save_vault_bindings,
)
from portunus.cli import main
from portunus.discover import list_gcp_secrets

REAUTH_STDERR = (
    "ERROR: (gcloud.secrets.versions.describe) There was a problem refreshing\n"
    "your current auth tokens: Reauthentication failed. cannot prompt during\n"
    "non-interactive execution.\n\nPlease run:\n\n  $ gcloud auth login\n"
)


class Recorder:
    def __init__(self, returncode=0, stdout="ok", stderr=""):
        self.calls = []
        self.returncode, self.stdout, self.stderr = returncode, stdout, stderr

    def __call__(self, cmd, **kwargs):
        self.calls.append((cmd, kwargs))
        return SimpleNamespace(returncode=self.returncode, stdout=self.stdout, stderr=self.stderr)


@pytest.fixture
def key_file(tmp_path):
    path = tmp_path / "sa-key.json"
    path.write_text('{"type": "service_account"}')
    os.chmod(path, 0o600)
    return path


# --- binding persistence --------------------------------------------------------
def test_credential_file_round_trips_and_defaults_empty(home, key_file):
    assert VaultBinding("p").credential_file == ""
    save_vault_bindings({"p": VaultBinding("p", credential_file=str(key_file))})
    assert load_vault_bindings()["p"].credential_file == str(key_file)


# --- backend identity selection ----------------------------------------------
def test_credential_file_sets_override_env_and_skips_account(home, gcloud_on_path, key_file):
    runner = Recorder()
    binding = VaultBinding("demo", account="me@example.com", credential_file=str(key_file))
    backend = GcloudBackend(bindings={"demo": binding}, runner=runner, audit=AuditChain())
    backend.access("sm-x", project="demo")
    cmd, kwargs = runner.calls[0]
    assert kwargs["env"]["CLOUDSDK_AUTH_CREDENTIAL_FILE_OVERRIDE"] == str(key_file)
    assert not any(a.startswith("--account=") for a in cmd)
    # the key's contents never reach argv
    assert "service_account" not in " ".join(cmd)


def test_credential_file_composes_with_impersonation(home, gcloud_on_path, key_file):
    runner = Recorder(stdout='{"createTime": "t"}')
    binding = VaultBinding("demo", credential_file=str(key_file),
                           impersonate_service_account="sa@demo.iam.gserviceaccount.com")
    GcloudBackend(bindings={"demo": binding}, runner=runner).latest_version("sm", project="demo")
    cmd, kwargs = runner.calls[0]
    assert "--impersonate-service-account=sa@demo.iam.gserviceaccount.com" in cmd
    assert "CLOUDSDK_AUTH_CREDENTIAL_FILE_OVERRIDE" in kwargs["env"]


def test_add_version_with_credential_file_keeps_value_on_stdin(home, gcloud_on_path, key_file):
    runner = Recorder(stdout="projects/demo/secrets/sm/versions/2")
    binding = VaultBinding("demo", credential_file=str(key_file))
    GcloudBackend(bindings={"demo": binding}, runner=runner).add_version("sm", "s3cret", project="demo")
    cmd, kwargs = runner.calls[0]
    assert kwargs["input"] == "s3cret" and "s3cret" not in cmd
    assert "CLOUDSDK_AUTH_CREDENTIAL_FILE_OVERRIDE" in kwargs["env"]


def test_missing_credential_file_fails_closed_without_calling_gcloud(home, gcloud_on_path, tmp_path):
    runner = Recorder()
    binding = VaultBinding("demo", credential_file=str(tmp_path / "gone.json"))
    with pytest.raises(BackendError, match="credential_file for demo not found"):
        GcloudBackend(bindings={"demo": binding}, runner=runner).access("sm", project="demo")
    assert runner.calls == []


def test_account_binding_passes_no_env(home, gcloud_on_path):
    runner = Recorder()
    binding = VaultBinding("demo", account="me@example.com")
    GcloudBackend(bindings={"demo": binding}, runner=runner).access("sm", project="demo")
    assert "env" not in runner.calls[0][1]


# --- reauth classification ----------------------------------------------------
def test_reauth_stderr_is_classified():
    assert is_gcloud_reauth_error(REAUTH_STDERR)
    assert not is_gcloud_reauth_error("ERROR: NOT_FOUND: Secret [x] not found")


def test_reauth_failure_raises_with_noninteractive_remediation(home, gcloud_on_path):
    runner = Recorder(returncode=1, stdout="", stderr=REAUTH_STDERR)
    binding = VaultBinding("demo", account="me@example.com")
    backend = GcloudBackend(bindings={"demo": binding}, runner=runner)
    with pytest.raises(GcpReauthRequired) as exc:
        backend.latest_version("sm", project="demo")
    msg = str(exc.value)
    assert "portunus bindings set demo --credential-file" in msg
    assert "--impersonate-service-account" in msg
    assert "gcloud auth login me@example.com --no-activate" in msg


def test_non_reauth_failure_stays_plain_backend_error(home, gcloud_on_path):
    runner = Recorder(returncode=1, stdout="", stderr="PERMISSION_DENIED")
    with pytest.raises(BackendError) as exc:
        GcloudBackend(project="demo", runner=runner).access("sm")
    assert not isinstance(exc.value, GcpReauthRequired)


def test_remediation_names_no_credential():
    text = gcp_reauth_remediation("demo", VaultBinding("demo", account="me@example.com"))
    assert "demo" in text and "me@example.com" in text


# --- health -------------------------------------------------------------------
@pytest.fixture
def gcp_health_home(home, monkeypatch):
    monkeypatch.delenv("PORTUNUS_BACKEND", raising=False)
    monkeypatch.delenv("PORTUNUS_GCP_PROJECT", raising=False)
    os.chmod(home, 0o700)
    monkeypatch.setattr(health_mod.shutil, "which", lambda _: "/usr/bin/gcloud")
    Registry().add("gcp-token", "gcp-token-sm", project="demo")
    return home


def _gcp_check(result):
    return next(c for c in result["checks"] if c["name"] == "backend:gcp")


def test_health_reauth_failure_carries_self_service_fix(gcp_health_home):
    save_vault_bindings({"demo": VaultBinding("demo", account="me@example.com")})

    def runner(cmd, **kwargs):
        return subprocess.CompletedProcess(cmd, 1, "", REAUTH_STDERR)

    check = _gcp_check(health_mod.run_health(runner=runner))
    assert check["ok"] is False
    assert "portunus bindings set demo --credential-file" in check["detail"]


def test_health_ok_on_user_credential_warns_about_reauth(gcp_health_home):
    save_vault_bindings({"demo": VaultBinding("demo", account="me@example.com")})

    def runner(cmd, **kwargs):
        return subprocess.CompletedProcess(cmd, 0, '{"createTime": "t"}', "")

    check = _gcp_check(health_mod.run_health(runner=runner))
    assert check["ok"] is True
    assert "can expire into an interactive reauth" in check["detail"]


def test_health_ok_on_credential_file_has_no_warning(gcp_health_home, key_file):
    save_vault_bindings({"demo": VaultBinding("demo", credential_file=str(key_file))})
    seen = []

    def runner(cmd, **kwargs):
        seen.append(kwargs.get("env", {}).get("CLOUDSDK_AUTH_CREDENTIAL_FILE_OVERRIDE"))
        return subprocess.CompletedProcess(cmd, 0, '{"createTime": "t"}', "")

    check = _gcp_check(health_mod.run_health(runner=runner))
    assert check["ok"] is True
    assert "reauth" not in check["detail"]
    assert seen == [str(key_file)]


# --- CLI ----------------------------------------------------------------------
def test_bindings_set_credential_file_stores_absolute_path(home, key_file, capsys):
    assert main(["bindings", "set", "demo", "--credential-file", str(key_file)]) == 0
    assert load_vault_bindings()["demo"].credential_file == str(key_file.resolve())
    assert main(["bindings", "show", "demo", "--json"]) == 0
    out = capsys.readouterr().out.strip().splitlines()[-1]
    assert json.loads(out)["demo"]["credential_file"] == str(key_file.resolve())


def test_bindings_set_credential_file_rejects_missing_file(home, tmp_path):
    assert main(["bindings", "set", "demo", "--credential-file", str(tmp_path / "nope")]) != 0
    assert "demo" not in load_vault_bindings()


def test_bindings_set_credential_file_warns_on_loose_mode(home, key_file, capsys):
    os.chmod(key_file, 0o644)
    assert main(["bindings", "set", "demo", "--credential-file", str(key_file)]) == 0
    assert "chmod 600" in capsys.readouterr().err


def test_bindings_set_credential_file_none_clears(home, key_file):
    main(["bindings", "set", "demo", "--credential-file", str(key_file)])
    assert main(["bindings", "set", "demo", "--credential-file", "none"]) == 0
    assert load_vault_bindings()["demo"].credential_file == ""


def test_auth_status_reports_credential_file_mode(home, key_file, monkeypatch, capsys, gcloud_on_path):
    save_vault_bindings({"demo": VaultBinding("demo", credential_file=str(key_file))})
    seen = []

    def fake_run(cmd, capture_output, text, timeout, env=None):
        seen.append((cmd, env))
        return SimpleNamespace(returncode=0, stdout="ya29.FAKE", stderr="")

    monkeypatch.setattr("portunus.cli.subprocess.run", fake_run)
    assert main(["auth", "status", "--json"]) == 0
    out = json.loads(capsys.readouterr().out)
    assert out["demo"]["mode"] == "credential-file"
    assert out["demo"]["state"] == "ok"
    cmd, env = seen[0]
    assert not any(a.startswith("--account=") for a in cmd)
    assert env["CLOUDSDK_AUTH_CREDENTIAL_FILE_OVERRIDE"] == str(key_file)


def test_auth_status_missing_credential_file(home, tmp_path, capsys, gcloud_on_path):
    save_vault_bindings({"demo": VaultBinding("demo", credential_file=str(tmp_path / "gone"))})
    assert main(["auth", "status", "--strict"]) == 1
    assert "--credential-file" in capsys.readouterr().out


# --- discover -----------------------------------------------------------------
def test_discover_honors_credential_file(gcloud_on_path, key_file):
    runner = Recorder(stdout="[]")
    list_gcp_secrets("demo", account="me@example.com", credential_file=str(key_file), runner=runner)
    cmd, kwargs = runner.calls[0]
    assert not any(a.startswith("--account=") for a in cmd)
    assert kwargs["env"]["CLOUDSDK_AUTH_CREDENTIAL_FILE_OVERRIDE"] == str(key_file)
