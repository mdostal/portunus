"""`portunus sync <project>` (story 04, portunus-vault-routing)."""
import json
from types import SimpleNamespace

from portunus import Registry
from portunus.backend import VaultBinding, save_vault_bindings
from portunus.cli import main


def _mock_gcloud(monkeypatch, responses):
    calls = []

    def fake_run(cmd, capture_output, text, timeout):
        calls.append(cmd)
        rc, out, err = responses.pop(0)
        return SimpleNamespace(returncode=rc, stdout=out, stderr=err)

    monkeypatch.setattr("portunus.backend.subprocess.run", fake_run)
    monkeypatch.setattr("portunus.backend.shutil.which", lambda name: "/bin/gcloud")
    return calls


def test_sync_reports_synced_and_fresh(home, monkeypatch, capsys):
    _mock_gcloud(monkeypatch, [
        (0, json.dumps({"name": "v1", "createTime": "T1"}), ""),  # x: describe
        (0, "VALUE-X", ""),                                        # x: fetch
        (0, json.dumps({"name": "v1", "createTime": "T1"}), ""),  # y: describe
        (0, "VALUE-Y", ""),                                        # y: fetch
    ])
    save_vault_bindings({"demo": VaultBinding("demo", backend="gcp", sync_mode="cached")})
    reg = Registry()
    reg.add("x", "sm-x", project="demo")
    reg.add("y", "sm-y", project="demo")

    rc = main(["sync", "demo", "--json"])
    out = capsys.readouterr().out
    assert rc == 0
    data = json.loads(out)
    assert set(data["synced"]) == {"x", "y"}
    assert data["already_fresh"] == []
    assert data["failed"] == []


def test_sync_second_run_reports_already_fresh(home, monkeypatch, capsys):
    _mock_gcloud(monkeypatch, [
        (0, json.dumps({"name": "v1", "createTime": "T1"}), ""),
        (0, "VALUE-X", ""),
        (0, json.dumps({"name": "v1", "createTime": "T1"}), ""),  # second run's describe only
    ])
    save_vault_bindings({"demo": VaultBinding("demo", backend="gcp", sync_mode="cached")})
    Registry().add("x", "sm-x", project="demo")

    main(["sync", "demo", "--json"])
    capsys.readouterr()
    rc = main(["sync", "demo", "--json"])
    out = capsys.readouterr().out
    assert rc == 0
    data = json.loads(out)
    assert data["synced"] == []
    assert data["already_fresh"] == ["x"]


def test_sync_no_cached_references_reports_cleanly(home, capsys):
    save_vault_bindings({"demo": VaultBinding("demo", backend="gcp", sync_mode="direct")})
    Registry().add("x", "sm-x", project="demo")

    rc = main(["sync", "demo"])
    out = capsys.readouterr().out
    assert rc == 0
    assert "no cached-mode references" in out.lower()


def test_sync_partial_failure_does_not_abort_the_batch(home, monkeypatch, capsys):
    responses = [
        (0, json.dumps({"name": "v1", "createTime": "T1"}), ""),  # x: describe ok
        (1, "", "gcloud: permission denied"),                      # x: fetch fails
        (0, json.dumps({"name": "v1", "createTime": "T1"}), ""),  # y: describe ok
        (0, "VALUE-Y", ""),                                         # y: fetch ok
    ]
    _mock_gcloud(monkeypatch, responses)
    save_vault_bindings({"demo": VaultBinding("demo", backend="gcp", sync_mode="cached")})
    reg = Registry()
    reg.add("x", "sm-x", project="demo")
    reg.add("y", "sm-y", project="demo")

    rc = main(["sync", "demo", "--json"])
    out = capsys.readouterr().out
    assert rc == 0
    data = json.loads(out)
    assert data["synced"] == ["y"]
    assert len(data["failed"]) == 1
    assert data["failed"][0]["name"] == "x"
    assert "permission denied" in data["failed"][0]["error"]
