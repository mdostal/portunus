"""`portunus drop-bulk <file.json>` (story 06, portunus-vault-routing)."""
import json

from portunus import Registry
from portunus.cli import main


def test_drop_bulk_creates_all_valid_entries(home, tmp_path, capsys):
    entries = [
        {"name": "a", "sm_name": "sm-a", "value": "va"},
        {"name": "b", "sm_name": "sm-b", "value": "vb"},
    ]
    entries_file = tmp_path / "entries.json"
    entries_file.write_text(json.dumps(entries))

    rc = main(["drop-bulk", str(entries_file), "--json"])
    out = capsys.readouterr().out
    assert rc == 0
    data = json.loads(out)
    assert set(data["created"]) == {"a", "b"}
    assert data["failed"] == []
    assert Registry().require("a").state == "dropped"


def test_drop_bulk_isolates_failures(home, tmp_path, capsys):
    entries = [
        {"name": "a", "sm_name": "sm-a", "value": "va"},
        {"name": "bad", "sm_name": "sm-bad", "value": ""},
    ]
    entries_file = tmp_path / "entries.json"
    entries_file.write_text(json.dumps(entries))

    rc = main(["drop-bulk", str(entries_file), "--json"])
    out = capsys.readouterr().out
    assert rc == 0
    data = json.loads(out)
    assert data["created"] == ["a"]
    assert len(data["failed"]) == 1
    assert data["failed"][0]["name"] == "bad"


def test_drop_bulk_backend_gate(home, tmp_path, monkeypatch, capsys):
    monkeypatch.setenv("PORTUNUS_BACKEND", "gcloud")
    entries_file = tmp_path / "entries.json"
    entries_file.write_text(json.dumps([{"name": "a", "sm_name": "sm-a", "value": "va"}]))

    rc = main(["drop-bulk", str(entries_file)])
    err = capsys.readouterr().err
    assert rc == 1
    assert "local-encrypted backend" in err
