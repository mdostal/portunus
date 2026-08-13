"""portunus discover --provider gcp --project ... (story 04)."""
import json

from portunus import Registry
from portunus.cli import main


def _mock_gcloud_list(monkeypatch, secrets):
    from types import SimpleNamespace

    def fake_run(cmd, capture_output, text, timeout):
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
