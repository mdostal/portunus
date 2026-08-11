import json
from portunus.cli import main

def test_session_cli_save_load_list_revoke(tmp_path, monkeypatch, capsys):
    monkeypatch.setenv("PORTUNUS_HOME", str(tmp_path))
    
    # Save
    session_data = {"cookies": [{"name": "foo", "value": "bar"}]}
    monkeypatch.setattr("sys.stdin.read", lambda: json.dumps(session_data))
    
    argv = ["session", "save", "example.com", "user1", "--stdin"]
    assert main(argv) == 0
    out, err = capsys.readouterr()
    res = json.loads(out)
    assert res["namespace"]["site"] == "example.com"
    
    # Load
    argv = ["session", "load", "example.com", "user1"]
    assert main(argv) == 0
    out, err = capsys.readouterr()
    res = json.loads(out)
    assert res["session"]["cookies"][0]["value"] == "bar"
    
    # List
    argv = ["session", "list"]
    assert main(argv) == 0
    out, err = capsys.readouterr()
    res = json.loads(out)
    assert len(res) == 1
    assert res[0]["namespace"]["account"] == "user1"
    
    # Revoke
    argv = ["session", "revoke", "example.com", "user1"]
    assert main(argv) == 0
    out, err = capsys.readouterr()
    assert "revoked session for example.com user1" in out
    
    # List again
    argv = ["session", "list"]
    assert main(argv) == 0
    out, err = capsys.readouterr()
    res = json.loads(out)
    assert len(res) == 0
