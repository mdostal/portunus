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
import secrets
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Literal, Optional

from . import roles
from .audit import AuditChain
from .paths import home
from .registry import Registry, Reference


class NotInjectable(RuntimeError):
    """Raised when a reference's lifecycle state forbids injection."""


class ApprovalRequired(RuntimeError):
    """Raised when a gated reference has no valid (unexpired) approval."""


class NotAuthorized(RuntimeError):
    """Raised when enforcement is on (portunus-petitio-rbac Story 03,
    `portunus roles enforce on`) and roles.evaluate() denies the requester.
    Distinct from NotInjectable (wrong lifecycle state) and ApprovalRequired
    (needs a human approval) -- this is specifically "wrong identity/scope"."""


@dataclass(frozen=True)
class Identity:
    """A principal that might request a secret -- the seam Petitio's future
    access-level enforcement will consume. Not enforced anywhere yet (see
    Broker.check_injectable's requester parameter); this exists so every
    call site already threads an identity through, ahead of the policy
    engine that will eventually read it.

    from_env() resolves the same way AuditChain's own actor resolution
    already does (DOSTAL_AGENT for agents, USER for humans) -- no new
    resolution mechanism, reuses the existing seed.
    """

    name: str
    kind: Literal["human", "agent", "system"]

    @staticmethod
    def from_env() -> "Identity":
        agent = os.environ.get("DOSTAL_AGENT")
        if agent:
            return Identity(name=agent, kind="agent")
        return Identity(name=os.environ.get("USER", "unknown"), kind="human")


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
    def check_injectable(self, name: str, requester: Optional[Identity] = None,
                         consume: bool = True) -> Reference:
        """Return the Reference iff it may be injected right now, else raise.

        Enforces the lifecycle state and the approval gate. This is the
        single chokepoint the resolver calls before any value is fetched.

        `requester` feeds a policy evaluation (portunus-petitio-rbac
        Story 02, design-discussion.md §4/§6): every call with a real
        `requester` gets a `would-allow`/`would-deny` audit line reflecting
        what `roles.evaluate()` decided. Whether that decision can also
        raise `NotAuthorized` depends entirely on
        `roles.enforcement_is_on()` (Story 03, `portunus roles enforce`) --
        default off, so a fresh vault's behavior is unchanged. Even with
        enforcement on, a scope with zero configured policies still always
        allows -- enforcement only ever denies within a scope that has at
        least one policy, for a principal not named there
        (design-discussion.md §5, "permissive-if-unconfigured").

        A single-use approval (an approved request, GH #251) is used up by
        the call that passes the gate. Bulk callers that only look (leak
        scan, sync) pass ``consume=False``: a single-use approval then
        counts as no approval, so they can neither use it up nor read
        through it.
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
        if ref.approval == "required" and not self._has_valid_approval(name, consume=consume):
            self.audit.append("resolve", ref.sm_name, "pending-approval")
            raise ApprovalRequired(
                f"{ref.sm_name} needs approval — a human must run: portunus approve {name}"
            )
        if requester is not None:
            decision = roles.evaluate(roles.load_policies(), requester, ref)
            verb = "would-allow" if decision.allow else "would-deny"
            self.audit.append("resolve", ref.sm_name, f"{verb}:{decision.reason}")
            if not decision.allow and roles.enforcement_is_on():
                raise NotAuthorized(
                    f"{ref.sm_name} is not authorized for {requester.name} "
                    f"— no policy record grants this scope to this identity"
                )
        return ref

    # --- session access gate (portunus-session-access-gate) --------------
    def check_session_access(
        self,
        site: str,
        account: str,
        *,
        org: str = "",
        project: str = "",
        env: str = "",
        repo: str = "",
        requester: Optional[Identity] = None,
    ) -> None:
        """Gate a session's fetch boundary (`session load`) through the
        exact same roles.evaluate() seam check_injectable() already uses.
        A session isn't a Registry Reference (no sm_name/state/approval
        lifecycle) -- roles.evaluate()/_scope_matches() are fully
        duck-typed, so a synthetic, in-memory Reference (never persisted)
        is all that's needed, no second gating mechanism.

        Same posture as check_injectable() exactly: `requester is None`
        fails open; a real requester always gets a would-allow/would-deny
        audit line; NotAuthorized is raised only when the decision denies
        AND roles.enforcement_is_on() (permissive-if-unconfigured holds
        the same way). Only `session load` calls this -- store/remove/
        inspect/list stay ungated, mirroring check_injectable's own
        write/metadata-view exemption (drop/retag/reg show aren't gated
        either).
        """
        if requester is None:
            return
        synthetic_ref = Reference(
            name=f"session:{site}:{account}", sm_name="",
            org=org, project=project, env=env, repo=repo,
        )
        decision = roles.evaluate(roles.load_policies(), requester, synthetic_ref)
        verb = "would-allow" if decision.allow else "would-deny"
        self.audit.append("session_access", f"{site}:{account}", f"{verb}:{decision.reason}")
        if not decision.allow and roles.enforcement_is_on():
            raise NotAuthorized(
                f"session {site}/{account} is not authorized for {requester.name} "
                f"— no policy record grants this scope to this identity"
            )

    # --- gate / approve --------------------------------------------------
    def gate(self, name: str, on: bool = True) -> Reference:
        ref = self.registry.set_approval(name, on)
        self.audit.append("gate", ref.sm_name, "required" if on else "off")
        return ref

    def approve(self, name: str, ttl: int = 3, approver: Optional[str] = None,
                ttl_seconds: Optional[int] = None, once: bool = False,
                requester: str = "", channel: str = "", request_id: str = "",
                factor: str = "") -> Path:
        """Grant a time-boxed approval. By default it lasts `ttl` audit-clock
        ticks; pass `ttl_seconds` instead for a wall-clock window (e.g. a
        two-hour sudo session), which isn't capped by access count.

        `once` makes it single-use: exactly one resolve passes the gate,
        then the approval is gone. `requester` binds it to that requester
        (see approval.requester_name); anyone else is refused. `channel`,
        `request_id` and `factor` are recorded in the audit entry."""
        ref = self.registry.require(name)
        approver = approver or os.environ.get("USER", "unknown")
        token = self.approvals_dir / name
        if ttl_seconds is not None:
            expiry = f"exp_at={int(time.time()) + int(ttl_seconds)}"
        else:
            expiry = f"exp={self._clock_now() + ttl}"
        fields = [f"approved_by={approver}", expiry]
        if once:
            fields.append("uses=1")
        if requester:
            fields.append(f"for={requester}")
        if request_id:
            fields.append(f"req={request_id}")
        tmp = self.approvals_dir / f".{name}.{secrets.token_hex(4)}.tmp"
        tmp.write_text(" ".join(fields) + "\n")
        os.chmod(tmp, 0o600)
        os.replace(tmp, token)
        result = f"approved:{approver}"
        for key, val in (("requester", requester), ("channel", channel),
                         ("request", request_id), ("factor", factor)):
            if val:
                result += f" {key}:{val}"
        if once:
            result += " uses:1"
        self.audit.append("approve", ref.sm_name, result)
        return token

    @staticmethod
    def _parse_token(text: str) -> dict:
        out: dict = {}
        for part in text.split():
            key, _, raw = part.partition("=")
            out[key] = raw
        return out

    def _has_valid_approval(self, name: str, consume: bool = True) -> bool:
        from .approval import requester_name

        token = self.approvals_dir / name
        try:
            fields = self._parse_token(token.read_text())
        except OSError:
            return False
        try:
            exp = int(fields.get("exp", "0"))
            exp_at = int(fields["exp_at"]) if "exp_at" in fields else None
            uses = int(fields["uses"]) if "uses" in fields else None
        except ValueError:
            token.unlink(missing_ok=True)
            return False
        if exp_at is not None:
            live = time.time() <= exp_at
        else:
            live = exp >= self._clock_now()
        if not live:
            token.unlink(missing_ok=True)
            return False
        bound = fields.get("for", "")
        if bound and bound != requester_name():
            ref = self.registry.get(name)
            self.audit.append("resolve", ref.sm_name if ref else name,
                              f"approval-not-for:{requester_name()}")
            return False
        if uses is None:
            return True
        if not consume:
            return False
        # Single use: claim the token with an atomic rename so that of two
        # concurrent resolves exactly one gets through.
        claimed = self.approvals_dir / f".{name}.claimed.{os.getpid()}.{secrets.token_hex(4)}"
        try:
            os.rename(token, claimed)
        except OSError:
            return False
        claimed.unlink(missing_ok=True)
        return True

    def _clock_now(self) -> int:
        # Same recovery as AuditChain._tick(): a missing/corrupt .clock falls
        # back to the log's highest seq, so approval expiry doesn't shift.
        return self.audit.current_seq()

    # --- grant -----------------------------------------------------------
    def grant(self, name: str, member: str) -> Reference:
        """Record an intended widening of access in the audit log -- ONLY.

        This makes no IAM change anywhere: it does not call ``gcloud secrets
        add-iam-policy-binding`` or any other provider API, and it does not
        touch roles.json. The ``grant`` audit line records intent; applying
        the actual permission is up to the caller.
        """
        ref = self.registry.require(name)
        self.audit.append("grant", ref.sm_name, f"granted:{member}")
        return ref
