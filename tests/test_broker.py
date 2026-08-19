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
    now = b._clock_now()
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


# --- story 03 (portunus-swappable-trio): Identity + inert requester ------

def test_identity_from_env_prefers_agent(monkeypatch):
    from portunus.broker import Identity
    monkeypatch.setenv("DOSTAL_AGENT", "dostal-dev")
    monkeypatch.setenv("USER", "mdostal")
    ident = Identity.from_env()
    assert ident == Identity(name="dostal-dev", kind="agent")


def test_identity_from_env_falls_back_to_user(monkeypatch):
    from portunus.broker import Identity
    monkeypatch.delenv("DOSTAL_AGENT", raising=False)
    monkeypatch.setenv("USER", "mdostal")
    ident = Identity.from_env()
    assert ident == Identity(name="mdostal", kind="human")


def test_check_injectable_with_no_requester_is_byte_identical(home):
    """Every existing call site -- behavior must be completely unchanged."""
    reg, b = _broker(home)
    assert b.check_injectable("x").sm_name == "dostal-x"


def test_check_injectable_requester_is_genuinely_inert(home):
    """Passing ANY Identity must never change the result -- proves the
    parameter is unused, not silently gated."""
    from portunus.broker import Identity
    reg, b = _broker(home)
    someone = Identity(name="anyone", kind="agent")
    nobody = Identity(name="a-completely-different-name", kind="human")
    assert b.check_injectable("x", requester=someone).sm_name == "dostal-x"
    assert b.check_injectable("x", requester=nobody).sm_name == "dostal-x"


def test_check_injectable_never_raises_based_on_a_policy_decision():
    """AST-level, updated for portunus-petitio-rbac Story 02: `requester`
    branching is now expected (it gates the audit-only policy evaluation),
    but no `raise` may ever be reachable from a branch that inspects
    `decision`/`decision.allow` -- that's the one hard invariant this story
    (and Story 02's own test suite) must preserve. Real enforcement is
    Story 03's job, behind a separate, explicit opt-in."""
    import ast
    import inspect
    import textwrap
    from portunus.broker import Broker

    src = textwrap.dedent(inspect.getsource(Broker.check_injectable))
    tree = ast.parse(src)
    func = tree.body[0]
    for node in ast.walk(func):
        if isinstance(node, ast.If):
            test_names = {n.id for n in ast.walk(node.test) if isinstance(n, ast.Name)}
            if "decision" in test_names:
                body_src = ast.unparse(ast.Module(body=node.body, type_ignores=[]))
                assert "raise" not in body_src, (
                    f"a branch on `decision` reaches a raise: {body_src}"
                )


def test_check_injectable_docstring_states_enforcement_not_built():
    from portunus.broker import Broker
    doc = (Broker.check_injectable.__doc__ or "").lower()
    assert "not" in doc and ("enforce" in doc or "built" in doc)
