import pytest
from portunus.broker import SessionAccessDenied, SessionExpired

def test_check_session_access_role(home):
    from portunus.registry import Registry
    from portunus.broker import Broker
    broker = Broker(Registry())
    
    # Matching role
    record_owner = {"namespace": {"site": "x", "account": "y", "owner_role": "admin"}}
    broker.check_session_access("x", "y", record_owner, "admin")
    
    # Mismatching role
    with pytest.raises(SessionAccessDenied):
        broker.check_session_access("x", "y", record_owner, "dev")
        
    # No role
    record_no_owner = {"namespace": {"site": "x", "account": "y"}}
    broker.check_session_access("x", "y", record_no_owner, "dev")

def test_check_session_access_ttl(home):
    from portunus.registry import Registry
    from portunus.broker import Broker
    from datetime import datetime, timedelta, timezone
    broker = Broker(Registry())
    
    now = datetime.now(timezone.utc)
    expired = now - timedelta(seconds=1)
    valid = now + timedelta(seconds=100)
    
    record_expired = {"ttl": {"expires_at": expired.isoformat()}}
    with pytest.raises(SessionExpired):
        broker.check_session_access("x", "y", record_expired, "admin")

    record_valid = {"ttl": {"expires_at": valid.isoformat()}}
    broker.check_session_access("x", "y", record_valid, "admin")
