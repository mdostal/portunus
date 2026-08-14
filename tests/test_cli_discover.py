"""portunus discover --provider gcp --project ... (story 04)."""
import json

from portunus import Registry
from portunus.cli import main


def _mock_gcloud_list(monkeypatch, secrets, seen_cmds=None):
    from types import SimpleNamespace

    def fake_run(cmd, capture_output, text, timeout):
        if seen_cmds is not None:
            seen_cmds.append(cmd)
        return SimpleNamespace(returncode=0, stdout=json.dumps(secrets), stderr="")

    monkeypatch.setattr("portunus.discover._default_runner", fake_run)
    monkeypatch.setattr("portunus.discover.shutil.which", lambda name: "/bin/gcloud")


def test_discover_diff_only_writes_nothing(home, monkeypatch, capsys):
    _mock_gcloud_list(monkeypatch, [{"name": "projects/demo/secrets/API_KEY", "labels": {}}])
    rc = main(["discover", "--provider", "gcp", "--project", "demo"])
    out = capsys.readouterr().out
    assert rc == 0
    assert "not-registered" in out
    assert "API_KEY" in out
    reg = Registry()
    assert "demo-api_key" not in reg


def test_discover_register_writes_requested_state(home, monkeypatch, capsys):
    _mock_gcloud_list(monkeypatch, [
        {"name": "projects/demo/secrets/API_KEY", "labels": {"purpose": "billing"}}
    ])
    rc = main(["discover", "--provider", "gcp", "--project", "demo", "--register"])
    out = capsys.readouterr().out
    assert rc == 0
    assert "registered" in out
    ref = Registry().require("demo-api_key")
    assert ref.state == "requested"
    assert ref.description == "billing"


def test_discover_json_diff_only(home, monkeypatch, capsys):
    _mock_gcloud_list(monkeypatch, [{"name": "projects/demo/secrets/API_KEY", "labels": {}}])
    rc = main(["discover", "--provider", "gcp", "--project", "demo", "--json"])
    out = capsys.readouterr().out
    assert rc == 0
    data = json.loads(out)
    assert data["already_registered"] == []
    assert data["not_yet_registered"][0]["sm_name"] == "API_KEY"
    assert "wif_configured" in data


def test_discover_json_register(home, monkeypatch, capsys):
    _mock_gcloud_list(monkeypatch, [{"name": "projects/demo/secrets/API_KEY", "labels": {}}])
    rc = main(["discover", "--provider", "gcp", "--project", "demo", "--register", "--json"])
    out = capsys.readouterr().out
    assert rc == 0
    data = json.loads(out)
    assert data["registered"] == ["demo-api_key"]
    assert data["conflicts"] == []
    assert data["already_registered"] == []


def test_discover_json_wif_configured_true_when_binding_has_audience(home, monkeypatch, capsys):
    from portunus.backend import VaultBinding, save_vault_bindings
    save_vault_bindings({"demo": VaultBinding("demo", "//iam.googleapis.com/some/audience")})
    _mock_gcloud_list(monkeypatch, [])
    rc = main(["discover", "--provider", "gcp", "--project", "demo", "--json"])
    out = capsys.readouterr().out
    data = json.loads(out)
    assert data["wif_configured"] is True
    assert "some/audience" not in out
    assert "iam.googleapis.com" not in out


def test_discover_json_wif_configured_false_with_no_binding(home, monkeypatch, capsys):
    _mock_gcloud_list(monkeypatch, [])
    rc = main(["discover", "--provider", "gcp", "--project", "demo", "--json"])
    out = capsys.readouterr().out
    data = json.loads(out)
    assert data["wif_configured"] is False


def test_cmd_discover_passes_binding_account_to_list_gcp_secrets(home, monkeypatch, capsys):
    from portunus.backend import VaultBinding, save_vault_bindings
    save_vault_bindings({"demo": VaultBinding("demo", account="user@example.com")})
    seen_cmds = []
    _mock_gcloud_list(monkeypatch, [], seen_cmds=seen_cmds)
    rc = main(["discover", "--provider", "gcp", "--project", "demo"])
    assert rc == 0
    assert "--account=user@example.com" in seen_cmds[0]


def test_cmd_discover_no_binding_means_no_account_flag(home, monkeypatch, capsys):
    seen_cmds = []
    _mock_gcloud_list(monkeypatch, [], seen_cmds=seen_cmds)
    rc = main(["discover", "--provider", "gcp", "--project", "demo"])
    assert rc == 0
    assert not any(arg.startswith("--account=") for arg in seen_cmds[0])
