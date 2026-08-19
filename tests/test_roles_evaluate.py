"""portunus-petitio-rbac Story 02: PolicyRecord.principal + "repo" scope type
+ roles.evaluate() wired into check_injectable() as audit-only. NEVER raises
in this story -- that's Story 03's job. This extends the existing "stub, not
enforced" test discipline (test_check_injectable_and_retag_are_byte_identical_
with_or_without_roles_configured in test_roles.py) rather than breaking it."""
import ast
import inspect

import pytest

from portunus import AuditChain, Broker, Registry
from portunus.broker import Identity
from portunus.roles import (
    VALID_SCOPE_TYPES,
    Decision,
    PolicyRecord,
    delete_policy,
    evaluate,
    load_policies,
    set_policy,
)


# --- schema: principal field + key-collision fix ----------------------------

def test_policy_record_principal_defaults_to_empty_string():
    record = PolicyRecord(scope_type="org", scope_value="x", role="dev")
    assert record.principal == ""


def test_policy_key_includes_principal():
    record = PolicyRecord(scope_type="org", scope_value="x", role="dev", principal="claude-ffe")
    assert record.key == "org:x:dev:claude-ffe"


def test_policy_key_wildcard_principal_uses_star():
    record = PolicyRecord(scope_type="org", scope_value="x", role="dev", principal="")
    assert record.key == "org:x:dev:*"


def test_set_policy_two_different_principals_same_scope_and_role_both_persist(home):
    """The key-collision fix this story exists to make: without it, setting
    a policy for one principal would silently overwrite a different
    principal's policy under the same scope+role."""
    set_policy("org", "firefly-events", "dev", ["read"], principal="claude-ffe")
    set_policy("org", "firefly-events", "dev", ["read"], principal="codex-ffe")
    policies = load_policies()
    principals = {p.principal for p in policies.values() if p.scope_value == "firefly-events"}
    assert principals == {"claude-ffe", "codex-ffe"}
    assert len(policies) == 2


def test_set_policy_wildcard_still_overwrites_itself(home):
    """Existing behavior for wildcard (no-principal) records is unchanged --
    only distinct principals get distinct entries."""
    set_policy("project", "shindig", "admin", ["read"])
    set_policy("project", "shindig", "admin", ["read", "test"])
    policies = load_policies()
    matching = [p for p in policies.values() if p.scope_value == "shindig"]
    assert len(matching) == 1
    assert matching[0].actions == ["read", "test"]


def test_load_policies_backward_compatible_with_pre_principal_roles_json(home):
    """An existing roles.json written before this story (no `principal` key
    at all) must still load correctly, with principal defaulting to ""."""
    import json as jsonlib

    path = home / "roles.json"
    path.write_text(jsonlib.dumps({
        "org:firefly-events:dev": {
            "scope_type": "org", "scope_value": "firefly-events",
            "role": "dev", "actions": ["read"],
        }
    }))
    policies = load_policies()
    record = policies["org:firefly-events:dev"]
    assert record.principal == ""


def test_repo_is_a_valid_scope_type():
    assert "repo" in VALID_SCOPE_TYPES


def test_set_policy_accepts_repo_scope_type(home):
    record = set_policy("repo", "event-api", "dev", ["read"])
    assert record.scope_type == "repo"


def test_delete_policy_respects_principal(home):
    set_policy("org", "x", "dev", ["read"], principal="claude-ffe")
    set_policy("org", "x", "dev", ["read"], principal="codex-ffe")
    assert delete_policy("org", "x", "dev", principal="claude-ffe") is True
    remaining = load_policies()
    assert len(remaining) == 1
    assert list(remaining.values())[0].principal == "codex-ffe"


# --- roles.evaluate() --------------------------------------------------------

def _ref(**kwargs):
    from portunus.registry import Reference
    defaults = dict(name="x", sm_name="sm-x", state="enabled")
    defaults.update(kwargs)
    return Reference(**defaults)


def test_evaluate_no_matching_policy_allows_with_reason():
    decision = evaluate({}, Identity(name="claude-ffe", kind="agent"), _ref(org="firefly-events"))
    assert decision == Decision(allow=True, reason="no-policy-configured")


def test_evaluate_requester_none_allows_identically_to_no_policy():
    policies = {"k": PolicyRecord(scope_type="org", scope_value="firefly-events", role="dev", principal="claude-ffe")}
    decision = evaluate(policies, None, _ref(org="firefly-events"))
    assert decision == Decision(allow=True, reason="no-policy-configured")


def test_evaluate_matching_principal_allows():
    policies = {"k": PolicyRecord(scope_type="org", scope_value="firefly-events", role="dev", principal="claude-ffe")}
    decision = evaluate(policies, Identity(name="claude-ffe", kind="agent"), _ref(org="firefly-events"))
    assert decision == Decision(allow=True, reason="explicit-allow")


def test_evaluate_non_matching_principal_denies():
    policies = {"k": PolicyRecord(scope_type="org", scope_value="firefly-events", role="dev", principal="claude-ffe")}
    decision = evaluate(policies, Identity(name="codex-other", kind="agent"), _ref(org="firefly-events"))
    assert decision == Decision(allow=False, reason="not-in-scope")


def test_evaluate_wildcard_principal_allows_anyone():
    policies = {"k": PolicyRecord(scope_type="org", scope_value="firefly-events", role="dev", principal="")}
    decision = evaluate(policies, Identity(name="anyone-at-all", kind="agent"), _ref(org="firefly-events"))
    assert decision == Decision(allow=True, reason="explicit-allow")


def test_evaluate_matches_on_repo_scope():
    policies = {"k": PolicyRecord(scope_type="repo", scope_value="event-api", role="dev", principal="claude-ffe")}
    decision = evaluate(policies, Identity(name="claude-ffe", kind="agent"), _ref(repo="event-api"))
    assert decision == Decision(allow=True, reason="explicit-allow")


def test_evaluate_repo_scope_does_not_match_a_different_repo():
    policies = {"k": PolicyRecord(scope_type="repo", scope_value="event-api", role="dev", principal="claude-ffe")}
    decision = evaluate(policies, Identity(name="claude-ffe", kind="agent"), _ref(repo="other-repo"))
    assert decision == Decision(allow=True, reason="no-policy-configured")


def test_evaluate_flat_or_org_level_allow_wins_over_repo_level_silence():
    """Deliberate v1 design (design-discussion.md §3): a matching allow at
    ANY scope is sufficient -- no most-specific-wins narrowing yet."""
    policies = {
        "a": PolicyRecord(scope_type="org", scope_value="firefly-events", role="dev", principal="claude-ffe"),
        "b": PolicyRecord(scope_type="repo", scope_value="event-api", role="dev", principal="someone-else"),
    }
    decision = evaluate(
        policies, Identity(name="claude-ffe", kind="agent"),
        _ref(org="firefly-events", repo="event-api"),
    )
    assert decision == Decision(allow=True, reason="explicit-allow")


def test_evaluate_docstring_documents_flat_or_and_single_seam():
    doc = evaluate.__doc__ or ""
    assert "flat" in doc.lower() or "or" in doc.lower()
    assert "single" in doc.lower() or "seam" in doc.lower()


# --- check_injectable() audit-only wiring -----------------------------------

def test_check_injectable_never_raises_from_policy_evaluation(home):
    """The story's one hard invariant: this story is audit-only. A
    would-deny decision must never become an exception."""
    reg = Registry()
    reg.add("x", "sm-x", org="firefly-events", state="enabled")
    broker = Broker(reg, AuditChain())
    set_policy("org", "firefly-events", "dev", ["read"], principal="someone-else")
    ref = broker.check_injectable("x", requester=Identity(name="claude-ffe", kind="agent"))
    assert ref.name == "x"  # succeeded despite a would-deny decision


def test_check_injectable_audit_line_no_policy_configured(home):
    reg = Registry()
    reg.add("x", "sm-x", org="firefly-events", state="enabled")
    audit = AuditChain()
    broker = Broker(reg, audit)
    broker.check_injectable("x", requester=Identity(name="claude-ffe", kind="agent"))
    last = audit.entries()[-1]
    assert last["result"] == "would-allow:no-policy-configured"


def test_check_injectable_audit_line_explicit_allow(home):
    reg = Registry()
    reg.add("x", "sm-x", org="firefly-events", state="enabled")
    audit = AuditChain()
    broker = Broker(reg, audit)
    set_policy("org", "firefly-events", "dev", ["read"], principal="claude-ffe")
    broker.check_injectable("x", requester=Identity(name="claude-ffe", kind="agent"))
    last = audit.entries()[-1]
    assert last["result"] == "would-allow:explicit-allow"


def test_check_injectable_audit_line_would_deny_not_in_scope(home):
    reg = Registry()
    reg.add("x", "sm-x", org="firefly-events", state="enabled")
    audit = AuditChain()
    broker = Broker(reg, audit)
    set_policy("org", "firefly-events", "dev", ["read"], principal="claude-ffe")
    broker.check_injectable("x", requester=Identity(name="codex-other", kind="agent"))
    last = audit.entries()[-1]
    assert last["result"] == "would-deny:not-in-scope"


def test_check_injectable_writes_no_policy_audit_line_when_requester_is_none(home):
    """No requester -- nothing meaningful to log about an anonymous
    caller; behavior stays exactly as it was pre-this-story, including
    writing zero audit entries on a clean success path."""
    reg = Registry()
    reg.add("x", "sm-x", org="firefly-events", state="enabled")
    audit = AuditChain()
    broker = Broker(reg, audit)
    broker.check_injectable("x")
    assert audit.entries() == []


# --- structural guards -------------------------------------------------------

def test_evaluate_and_check_injectable_never_raise_based_on_decision_allow():
    import portunus.broker as broker_mod
    import portunus.roles as roles_mod

    for mod, fn_name in ((roles_mod, "evaluate"), (broker_mod, "Broker")):
        src = inspect.getsource(getattr(mod, fn_name))
        tree = ast.parse(src)
        for node in ast.walk(tree):
            if isinstance(node, ast.Raise) and node.exc is not None:
                call_src = ast.unparse(node)
                assert "decision" not in call_src.lower() or "NotAuthorized" not in call_src, (
                    f"found a raise referencing decision in {mod.__name__}.{fn_name} -- "
                    "this story must be audit-only, never raise on decision.allow"
                )


def test_no_call_site_reimplements_scope_matching_logic():
    """All scope/precedence logic must live inside roles.evaluate() alone --
    check_injectable() and every touched call site just call it and act on
    the Decision, never reimplement matching themselves. Verified via AST:
    none of these may reference scope_type/scope_value/principal fields
    directly (that's roles.evaluate()'s job alone)."""
    import portunus.broker as broker_mod

    src = inspect.getsource(broker_mod.Broker.check_injectable)
    for forbidden in ("scope_type", "scope_value", ".principal"):
        assert forbidden not in src, (
            f"check_injectable() references {forbidden!r} directly -- scope/precedence logic "
            "must live inside roles.evaluate() alone (design-discussion.md §3)"
        )
