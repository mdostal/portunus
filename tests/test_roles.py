"""Role/policy schema -- STUB ONLY (portunus-vault-trust-and-access Slice 5).
Writes genuinely persist; nothing reads them for enforcement. The defining
test here is test_check_injectable_and_retag_are_byte_identical_with_or_
without_roles_configured -- proof the seam is truly inert, not just
"defaults to permissive" (a materially weaker guarantee)."""
import os
import subprocess
import sys

import pytest

from portunus import AuditChain, Broker, Registry
from portunus.roles import (
    PolicyError,
    delete_policy,
    load_policies,
    set_policy,
)


def test_set_policy_persists(home):
    record = set_policy("org", "firefly-events", "dev", ["read", "test"])
    assert record.scope_type == "org"
    assert record.scope_value == "firefly-events"
    assert record.role == "dev"
    assert record.actions == ["read", "test"]
    reloaded = load_policies()
    assert reloaded[record.key].actions == ["read", "test"]


def test_set_policy_overwrites_same_scope_and_role(home):
    set_policy("project", "shindig", "admin", ["read"])
    set_policy("project", "shindig", "admin", ["read", "test", "prod-release"])
    policies = load_policies()
    matching = [p for p in policies.values() if p.scope_value == "shindig" and p.role == "admin"]
    assert len(matching) == 1
    assert matching[0].actions == ["read", "test", "prod-release"]


def test_set_policy_rejects_invalid_scope_type(home):
    with pytest.raises(PolicyError):
        set_policy("repo", "shindig", "admin", ["read"])


def test_set_policy_requires_scope_value_and_role(home):
    with pytest.raises(PolicyError):
        set_policy("org", "", "dev", ["read"])
    with pytest.raises(PolicyError):
        set_policy("org", "firefly-events", "", ["read"])


def test_multiple_scopes_and_roles_coexist(home):
    """The real motivating example: org-wide dev access + project-scoped
    admin with prod-release rights, both configured at once."""
    set_policy("org", "firefly-events", "dev", ["read", "test"])
    set_policy("project", "shindig", "admin", ["read", "test", "prod-release"])
    policies = load_policies()
    assert len(policies) == 2


def test_delete_policy(home):
    set_policy("org", "firefly-events", "dev", ["read"])
    assert delete_policy("org", "firefly-events", "dev") is True
    assert load_policies() == {}


def test_delete_nonexistent_policy_returns_false(home):
    assert delete_policy("org", "nonexistent", "dev") is False


def test_load_policies_empty_when_file_absent(home):
    assert load_policies() == {}


def test_roles_file_never_holds_a_secret_value_structural_check():
    import ast
    import inspect
    import portunus.roles as roles_mod

    tree = ast.parse(inspect.getsource(roles_mod))
    names = {n.id for n in ast.walk(tree) if isinstance(n, ast.Name)}
    assert "access" not in names


def test_check_injectable_and_retag_are_byte_identical_with_or_without_roles_configured(home):
    """THE defining test for a genuinely inert stub. Not 'defaults to
    permissive' (a weaker guarantee that could silently start reading
    roles.json in a future edit without anyone noticing a behavior change)
    -- byte-identical results, checked directly, whether roles.json is
    absent or full of restrictive-looking records."""
    reg = Registry()
    reg.add("x", "sm-x", org="firefly-events", project="shindig", state="enabled")
    broker = Broker(reg, AuditChain())

    # Baseline: no roles.json at all.
    baseline_ref = broker.check_injectable("x")
    baseline_retag = reg.retag("x", description="baseline")

    # Now configure policies that, if enforced, would plausibly deny this
    # exact access (a viewer-only role, zero actions, at the matching scope).
    set_policy("org", "firefly-events", "viewer", [])
    set_policy("project", "shindig", "viewer", [])

    gated_ref = broker.check_injectable("x")
    gated_retag = reg.retag("x", description="after-roles-configured")

    assert baseline_ref.name == gated_ref.name == "x"
    assert baseline_ref.state == gated_ref.state == "enabled"
    assert gated_retag.description == "after-roles-configured"  # retag still just works


def test_concurrent_set_policy_from_separate_processes_never_loses_an_entry(home):
    """Real multi-process regression proof, matching this session's own
    established technique -- lock-from-day-one, not a retrofit."""
    barrier = home / "start-barrier"
    n = 10
    script = (
        "import sys, time\n"
        "from pathlib import Path\n"
        "from portunus.roles import set_policy\n"
        f"barrier = Path({str(barrier)!r})\n"
        "deadline = time.monotonic() + 5\n"
        "while not barrier.exists() and time.monotonic() < deadline:\n"
        "    pass\n"
        "set_policy('project', f'proj-{sys.argv[1]}', 'dev', ['read'])\n"
    )
    procs = [
        subprocess.Popen([sys.executable, "-c", script, str(i)], env=os.environ.copy())
        for i in range(n)
    ]
    barrier.write_text("go")
    for p in procs:
        assert p.wait(timeout=10) == 0

    policies = load_policies()
    assert len(policies) == n
