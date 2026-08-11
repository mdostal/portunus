from portunus.broker import Broker
from portunus.registry import Registry
from portunus.audit import AuditChain
from portunus.localvault import LocalEncryptedBackend

def test_broker_session_methods(tmp_path, monkeypatch):
    monkeypatch.setenv("PORTUNUS_HOME", str(tmp_path))
    broker = Broker(Registry(), AuditChain())
    vault = LocalEncryptedBackend()
    
    session_data = {"key": "value"}
    # Save
    res = broker.session_save("test.com", "alice", session_data, vault)
    assert res["namespace"]["account"] == "alice"
    
    # Audit log should have it
    entries = broker.audit.entries()
    assert entries[-1]["action"] == "session-save"
    
    # Load
    loaded = broker.session_load("test.com", "alice", vault)
    assert loaded["session"]["key"] == "value"
    
    # List
    sessions = broker.session_list(vault)
    assert len(sessions) == 1
    assert sessions[0]["namespace"]["site"] == "test.com"
    
    # Revoke
    ok = broker.session_revoke("test.com", "alice", vault)
    assert ok is True
    
    # List again
    assert len(broker.session_list(vault)) == 0
