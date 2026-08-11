import json
import os
import pytest
from portunus.playwright import inject_session
from portunus.localvault import LocalEncryptedBackend
from portunus.cli import _build

def test_inject_session(home):
    registry, _, broker, resolver = _build()
    backend = resolver.backend
    assert isinstance(backend, LocalEncryptedBackend)

    test_session = {"cookies": [{"name": "auth", "value": "secret"}]}
    
    backend.store_session(
        "playwright.test",
        "user1",
        test_session,
        ttl_seconds=3600,
        owner_role="admin"
    )
    
    # Test valid access
    with inject_session("playwright.test", "user1", "admin") as path:
        assert os.path.exists(path)
        assert oct(os.stat(path).st_mode & 0o777) == "0o600"
        with open(path) as f:
            data = json.load(f)
            assert data == test_session
            
    # Test cleanup
    assert not os.path.exists(path)
    
    # Test invalid access
    from portunus.broker import SessionAccessDenied
    with pytest.raises(SessionAccessDenied):
        with inject_session("playwright.test", "user1", "hacker"):
            pass
