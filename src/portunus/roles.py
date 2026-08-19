"""Role/policy schema -- STUB ONLY. Petitio's future access-level engine
(portunus-vault-trust-and-access Slice 5), explicitly deferred by the user:
"the roles part can be stubbed, but is part of petito and is deferred."

This module persists policy records genuinely (writes really land in
PORTUNUS_HOME/roles.json, reads return exactly what was written) but is
consumed by NOTHING today. `Broker.check_injectable()` and
`Registry.retag()` are BYTE-IDENTICAL in behavior whether roles.json is
absent, empty, or full of records -- confirmed directly by
tests/test_roles.py's own stub-inertness test, not just asserted in a
docstring. A present, visible, inert seam, exactly like `Identity.requester`
(broker.py) already is for secret access -- extended here, in SHAPE only,
to hierarchy-scoped (org/project/env) metadata/state actions a future
policy engine will read.

Shape models the real example that motivated this (design-discussion.md
§1): "give dev access across the entirety of Firefly Events, but admin of
Shindig specifically, with only Shindig allowed a prod release" --

  [
    {"scope_type": "org", "scope_value": "firefly-events", "role": "dev", "actions": ["read", "test"]},
    {"scope_type": "project", "scope_value": "shindig", "role": "admin", "actions": ["read", "test", "prod-release"]}
  ]

Deliberately open-ended: `role` and `actions` are plain strings, not a
hardcoded enum -- role meaning is scope-dependent (an org-wide "dev" and a
project-level "admin" aren't the same shape of permission), which argues
against baking in one fixed vocabulary this early (design-discussion.md §1).
"""
from __future__ import annotations

import json
import os
from dataclasses import dataclass, field
from pathlib import Path
from typing import Dict, List, Optional

from .filelock import flock_path
from .paths import home

VALID_SCOPE_TYPES = ("org", "project", "env", "repo")


class PolicyError(RuntimeError):
    """Raised for an invalid policy operation (bad scope_type, unknown
    record on delete). Never carries a secret value -- policies only ever
    describe scope/role/actions, all plain config strings."""


@dataclass
class PolicyRecord:
    scope_type: str
    scope_value: str
    role: str
    actions: List[str] = field(default_factory=list)
    # "" / "*" = applies to everyone -- every pre-existing roles.json record
    # (written before this field existed) loads with principal="" and keeps
    # behaving as a wildcard, so this is additive, not a breaking migration.
    principal: str = ""

    @property
    def key(self) -> str:
        # Includes principal so two different principals scoped to the same
        # (scope_type, scope_value, role) are DISTINCT stored records --
        # without this, set_policy() for one principal would silently
        # overwrite a different principal's policy (portunus-petitio-rbac
        # design-discussion.md §2), exactly the "crossing over" failure this
        # epic exists to prevent, self-inflicted by its own storage key.
        return f"{self.scope_type}:{self.scope_value}:{self.role}:{self.principal or '*'}"


def _roles_path(path: Optional[Path] = None) -> Path:
    return path or (home() / "roles.json")


def _roles_lock_path(path: Optional[Path] = None) -> Path:
    return _roles_path(path).with_suffix(".lock")


def _load_unlocked(path: Optional[Path] = None) -> Dict[str, PolicyRecord]:
    roles_path = _roles_path(path)
    if not roles_path.exists():
        return {}
    raw = json.loads(roles_path.read_text() or "{}")
    return {
        key: PolicyRecord(
            scope_type=cfg.get("scope_type", ""),
            scope_value=cfg.get("scope_value", ""),
            role=cfg.get("role", ""),
            actions=list(cfg.get("actions", [])),
            principal=cfg.get("principal", ""),  # absent (pre-this-story file) -> "" (wildcard)
        )
        for key, cfg in raw.items()
    }


def _save_unlocked(policies: Dict[str, PolicyRecord], path: Optional[Path] = None) -> None:
    roles_path = _roles_path(path)
    roles_path.parent.mkdir(parents=True, exist_ok=True)
    raw = {
        key: {
            "scope_type": p.scope_type, "scope_value": p.scope_value,
            "role": p.role, "actions": p.actions, "principal": p.principal,
        }
        for key, p in policies.items()
    }
    tmp = roles_path.with_suffix(".json.tmp")
    tmp.write_text(json.dumps(raw, indent=2))
    os.chmod(tmp, 0o600)
    os.replace(tmp, roles_path)
    os.chmod(roles_path, 0o600)


def load_policies(path: Optional[Path] = None) -> Dict[str, PolicyRecord]:
    """Plain read -- missing file means no policies configured, returns {}.
    Unlocked, matching every other config-load's own posture (a reader
    never observes a torn write; os.replace() is atomic)."""
    return _load_unlocked(path)


def set_policy(
    scope_type: str, scope_value: str, role: str, actions: List[str],
    principal: str = "", path: Optional[Path] = None,
) -> PolicyRecord:
    """Create or overwrite one (scope_type, scope_value, role, principal)
    policy record's actions. Load -> mutate -> save under one flock
    acquisition, same discipline views.py's own mutators established -- no
    retrofit needed here, unlike vault-bindings.json's still-unfixed
    read-modify-write race."""
    if scope_type not in VALID_SCOPE_TYPES:
        raise PolicyError(f"invalid scope_type: {scope_type!r} (want one of {VALID_SCOPE_TYPES})")
    if not scope_value:
        raise PolicyError("scope_value is required")
    if not role:
        raise PolicyError("role is required")
    record = PolicyRecord(
        scope_type=scope_type, scope_value=scope_value, role=role,
        actions=list(actions), principal=principal,
    )
    with flock_path(_roles_lock_path(path)):
        policies = _load_unlocked(path)
        policies[record.key] = record
        _save_unlocked(policies, path)
        return record


def delete_policy(
    scope_type: str, scope_value: str, role: str,
    principal: str = "", path: Optional[Path] = None,
) -> bool:
    key = f"{scope_type}:{scope_value}:{role}:{principal or '*'}"
    with flock_path(_roles_lock_path(path)):
        policies = _load_unlocked(path)
        existed = policies.pop(key, None) is not None
        if existed:
            _save_unlocked(policies, path)
        return existed


@dataclass(frozen=True)
class Decision:
    """The one and only shape roles.evaluate() ever returns. `reason` is a
    fixed enum string -- no free text, no scope/value details -- so it's
    always safe to write straight into an audit line."""
    allow: bool
    reason: str  # "no-policy-configured" | "explicit-allow" | "not-in-scope"


def _scope_matches(policy: "PolicyRecord", ref: object) -> bool:
    return getattr(ref, policy.scope_type, "") == policy.scope_value


def evaluate(policies: Dict[str, "PolicyRecord"], requester: Optional[object], ref: object) -> "Decision":
    """The SINGLE place any scope/precedence logic for Petitio may live --
    check_injectable() and every CLI/MCP caller only ever call this and act
    on the returned Decision, never reimplement matching themselves
    (portunus-petitio-rbac design-discussion.md §3). That single-seam
    discipline is deliberate: the user confirmed flat-OR precedence is fine
    for v1 but wants the code to expect "fancier rule systems" later --
    keeping every call site free of matching logic of its own means a
    future precedence model (most-specific-wins, or an adopted engine like
    Casbin) is a change to this one function's internals, not a rewrite
    across every caller.

    Precedence today, as a deliberate v1 choice, not an oversight: a FLAT
    OR across every matching policy -- any one matching, allowing policy is
    sufficient. There is no most-specific-wins narrowing yet (an org-level
    allow is not narrowed by a repo-level policy that doesn't list the same
    principal). Revisit only if real usage produces an actual case that
    needs narrowing.

    `requester is None` is treated identically to "no policy configured" --
    fail-open at the identity layer, not fail-closed, so a caller that
    hasn't been threaded with an identity never gets a surprise denial from
    a missing parameter.
    """
    matching = [p for p in policies.values() if _scope_matches(p, ref)]
    if not matching:
        return Decision(allow=True, reason="no-policy-configured")
    if requester is None:
        return Decision(allow=True, reason="no-policy-configured")
    for p in matching:
        if p.principal in ("", "*", requester.name):
            return Decision(allow=True, reason="explicit-allow")
    return Decision(allow=False, reason="not-in-scope")
