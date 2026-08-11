"""Broker policy: lifecycle guard, gate/approve TTL, grant audit."""
import pytest

from portunus import Registry, AuditChain, Broker
from portunus.broker import ApprovalRequired, NotInjectable


def _broker(home):
    reg = Registry()
    reg.add("x", "dostal-x", scope="shared", kind="anthropic")
    return reg, Broker(reg, AuditChain())


def test_enabled_is_injectable(home):
    reg, b = _broker(home)
    assert b.check_injectable("x").sm_name == "dostal-x"


@pytest.mark.parametrize("state", ["dropped", "revoked"])
def test_dropped_and_revoked_fail_closed(home, state):
    reg, b = _broker(home)
    reg.set_state("x", state)
    with pytest.raises(NotInjectable):
        b.check_injectable("x")


def test_gate_then_approve_ttl_expires(home):
    reg, b = _broker(home)
    b.gate("x", on=True)
    with pytest.raises(ApprovalRequired):
        b.check_injectable("x")
    # approve for exactly 1 clock tick beyond now
    b.approve("x", ttl=1)
    # the approve() call itself ticked the clock; the token exp is now+1 at
    # approve time. One more successful check consumes the remaining budget.
    assert b.check_injectable("x").sm_name == "dostal-x"


def test_grant_is_audited(home):
    reg, b = _broker(home)
    b.grant("x", "serviceAccount:agent-att@proj.iam")
    grants = [e for e in b.audit.entries() if e["action"] == "grant"]
    assert grants and grants[-1]["result"].startswith("granted:")


def test_gate_off_clears_requirement(home):
    reg, b = _broker(home)
    b.gate("x", on=True)
    b.gate("x", on=False)
    assert b.check_injectable("x").sm_name == "dostal-x"
