"""Petitio — the approval-gate wrapper: grant / gate / approve + the lifecycle guard.

Petitio wraps every OSTIARIUS request so access is always gated.

Ported from the approval + lifecycle layer of ``bin/secrets``. The broker
owns the *policy* decisions; the resolver asks it one question before every
fetch: ``check_injectable(name)``. If the answer is no, nothing is fetched.

  * lifecycle guard  — dropped/revoked references fail closed
  * gate/approve     — a reference can require a time-boxed human approval
  * grant            — an explicit, audited widening of access

Every decision is written to the audit chain.
"""
from __future__ import annotations

import os
from pathlib import Path
from typing import Optional

from .audit import AuditChain
from .paths import home
from .registry import Registry, Reference


class NotInjectable(RuntimeError):
    """Raised when a reference's lifecycle state forbids injection."""


class ApprovalRequired(RuntimeError):
    """Raised when a gated reference has no valid (unexpired) approval."""


class Broker:
    def __init__(self, registry: Registry, audit: Optional[AuditChain] = None,
                 approvals_dir: Optional[Path] = None):
        self.registry = registry
        self.audit = audit or AuditChain()
        self.approvals_dir = Path(approvals_dir) if approvals_dir else home() / "approvals"
        self.approvals_dir.mkdir(parents=True, exist_ok=True)
        try:
            self.approvals_dir.chmod(0o700)
        except OSError:
            pass

    # --- lifecycle guard -------------------------------------------------
    def check_injectable(self, name: str) -> Reference:
        """Return the Reference iff it may be injected right now, else raise.

        Enforces both the lifecycle state and the approval gate. This is the
        single chokepoint the resolver calls before any value is fetched.
        """
        ref = self.registry.require(name)
        state = ref.state or "enabled"
        # Positive allowlist, not a denylist: any state other than
        # enabled/locked fails closed by default, including states added
        # after this check was written (e.g. "requested"). A denylist would
        # silently treat an unrecognized future state as injectable.
        if state not in ("enabled", "locked"):
            self.audit.append("resolve", ref.sm_name, f"denied-{state}")
            hints = {
                "dropped": "re-enable it",
                "requested": "a human must fulfill it via `portunus drop`",
            }
            raise NotInjectable(
                f"{ref.sm_name} is {state} — not injectable "
                f"({hints.get(state, 'contact the owner')})"
            )
        if ref.approval == "required" and not self._has_valid_approval(name):
            self.audit.append("resolve", ref.sm_name, "pending-approval")
            raise ApprovalRequired(
                f"{ref.sm_name} needs approval — a human must run: portunus approve {name}"
            )
        return ref

    # --- gate / approve --------------------------------------------------
    def gate(self, name: str, on: bool = True) -> Reference:
        ref = self.registry.set_approval(name, on)
        self.audit.append("gate", ref.sm_name, "required" if on else "off")
        return ref

    def approve(self, name: str, ttl: int = 3, approver: Optional[str] = None) -> Path:
        """Grant a time-boxed approval (measured in audit-clock ticks)."""
        ref = self.registry.require(name)
        approver = approver or os.environ.get("USER", "unknown")
        now = self._clock_now()
        token = self.approvals_dir / name
        token.write_text(f"approved_by={approver} exp={now + ttl}\n")
        os.chmod(token, 0o600)
        self.audit.append("approve", ref.sm_name, f"approved:{approver}")
        return token

    def _has_valid_approval(self, name: str) -> bool:
        token = self.approvals_dir / name
        if not token.exists():
            return False
        exp = 0
        for part in token.read_text().split():
            if part.startswith("exp="):
                try:
                    exp = int(part[4:])
                except ValueError:
                    exp = 0
        if exp >= self._clock_now():
            return True
        token.unlink(missing_ok=True)
        return False

    def _clock_now(self) -> int:
        try:
            return int(self.audit.clock_path.read_text().strip() or "0")
        except (OSError, ValueError):
            return 0

    # --- grant -----------------------------------------------------------
    def grant(self, name: str, member: str) -> Reference:
        """Record an explicit, audited widening of access.

        In production this shells to ``gcloud secrets add-iam-policy-binding``;
        here we always write the audit record so the chain reflects the intent
        even in environments without GCP.
        """
        ref = self.registry.require(name)
        self.audit.append("grant", ref.sm_name, f"granted:{member}")
        return ref
