# Design Discussion — portunus-petitio-rbac

## 0. Goal

Activate the two seams Portunus already shipped and proved inert — `Identity` (threaded through
`check_injectable`, currently unused by every real caller) and `roles.py`'s `PolicyRecord` store
(persisted, CLI/UI-managed, currently unenforced) — so that a resolve/inject call can actually be
denied when the requesting agent's identity isn't scoped to the reference being asked for. The
user's own framing: "we'll need to start cleanly adding the petito stuff so that we can bind the
right account and right repo access to the right agents and they aren't crossing over and doing
whatever the hell they want." Grounded in a 393-reference real vault with zero adversarial-actor
requirement — the target failure modes are honest mistakes and prompt-injected instructions, not
a compromised co-resident process (research-brief.md §4, Tier 3).

## 1. Two independently-shippable changes, not one

`requester=` exists as a parameter on `check_injectable()` but no real call site passes it
(research-brief.md §1, verified against source). Wiring `Identity.from_env()` through
`resolver.py`, `cli.py`, `mcp_server.py`, `leakscan.py` is pure plumbing with zero behavior
change — every existing test must still pass byte-identical. Only after that is genuinely done
does adding the policy check inside `check_injectable()` mean anything. Splitting these into
Story 01 (plumbing) and Story 02 (schema + audit-only evaluation) means Story 01 is trivially
reviewable in isolation — "does this change any observable output?" has a single correct
answer, no.

## 2. Schema: extend `PolicyRecord`, don't build a second store

```python
@dataclass
class PolicyRecord:
    scope_type: str            # "org" | "project" | "env" | "repo"  (repo is new)
    scope_value: str
    role: str
    actions: List[str] = field(default_factory=list)
    principal: str = ""        # "" / "*" = applies to everyone -- every existing record
                                # in every real roles.json stays valid and unchanged
```

`"repo"` joins `VALID_SCOPE_TYPES` because `Reference.repo` already exists, is already a
structured tag field, and is already exposed in `portunus tree --by repo` — this is a one-line
addition, not new plumbing (research-brief.md §5). `principal` defaults to `""`, which is
explicitly "no restriction" — this is what makes the change additive rather than a breaking
migration on a live, populated vault.

`portunus roles set` gains an optional `--principal <name>` flag; the Settings UI's roles form
gains a matching field, defaulting to blank ("applies to everyone").

**A real key-collision gap caught reading `roles.py` directly, not surfaced by any of the 12
research passes:** `PolicyRecord.key` is `f"{scope_type}:{scope_value}:{role}"` and `set_policy()`
uses it to overwrite-in-place (`policies[record.key] = record`). Adding `principal` without
changing `key` means setting a `dev` role for `firefly-events` scoped to `claude-ffe` would
silently overwrite (not add to) a previously-set `dev` role for `firefly-events` scoped to
`codex-ffe` — exactly the "crossing over" failure this epic exists to prevent, self-inflicted by
its own storage key. `key` must become
`f"{scope_type}:{scope_value}:{role}:{principal or '*'}"` so distinct principals under the same
scope+role are distinct stored records. Story 02 must test this explicitly (two principals, same
scope+role, both survive).

## 3. The evaluator: `roles.evaluate()`, ~20-30 lines, no new module

```python
@dataclass(frozen=True)
class Decision:
    allow: bool
    reason: str   # "no-policy-configured" | "explicit-allow" | "not-in-scope"

def evaluate(policies: List[PolicyRecord], requester: Optional[Identity], ref: Reference) -> Decision:
    matching = [p for p in policies if _scope_matches(p, ref)]
    if not matching:
        return Decision(allow=True, reason="no-policy-configured")
    if requester is None:
        return Decision(allow=True, reason="no-policy-configured")  # can't evaluate what we can't identify
    for p in matching:
        if p.principal in ("", "*", requester.name):
            return Decision(allow=True, reason="explicit-allow")
    return Decision(allow=False, reason="not-in-scope")
```

`_scope_matches` checks `org`/`project`/`env`/`repo` fields on `ref` against `scope_value` for
matching `scope_type`s (a reference can match more than one scope simultaneously — e.g. an
`org`-level policy AND a `repo`-level policy). `requester is None` behaves identically to
"no policy configured" — a caller that hasn't been threaded with an identity yet (there
shouldn't be any left after Story 01, but this keeps the function total, not partial) never gets
a surprise denial from a missing parameter.

**Scope precedence, decided explicitly rather than left implicit:** all matching policies are
evaluated as a flat OR — any one matching, allowing policy is sufficient. There is no
most-specific-wins narrowing in v1: if an `org`-level policy allows principal X but a
`repo`-level policy under that org does NOT list X, X is still allowed (the org-level grant
wins). This is deliberately the more permissive reading. A most-specific-wins/narrowing model
(where a repo-level policy can *restrict* what an org-level policy grants) is real and useful,
but adds a second axis of complexity — precedence ordering across four scope types — that
doesn't have a forcing real-world case yet in a vault where zero principal-scoped policies exist
today. Ship the simple OR model; revisit if real usage produces an actual case that needs
narrowing (see Open Questions).

## 4. Where the check lives

```python
def check_injectable(self, name, requester=None):
    ref = self.registry.require(name)
    # ...existing lifecycle guard (unchanged)...
    # ...existing approval gate (unchanged)...
    decision = roles.evaluate(roles.load_policies(self.home), requester, ref)
    self.audit.append("resolve", ref.sm_name, f"policy:{decision.reason}")
    if not decision.allow and enforcement_is_on(self.home):
        raise NotAuthorized(f"{ref.sm_name} is out of scope for {requester.name if requester else 'unknown'}")
    return ref
```

`NotAuthorized` is a new exception distinct from `NotInjectable`/`ApprovalRequired` so error
messages and callers can distinguish "wrong lifecycle state," "needs approval," and "wrong
identity/scope." The audit line is written on every call, allow or deny, once `requester` is
present — this is what makes Story 02's audit-only stage genuinely observable before enforcement
ever blocks anything real.

## 5. Default posture — the highest-stakes decision in this design

A scope with zero `PolicyRecord`s stays exactly as it behaves today: unconditional allow. Real
denial only happens within a scope that has at least one configured policy, for principals not
named there. Flipping the enforcement flag on the real 393-reference vault — which has zero
principal-scoped policies configured today — changes nothing until the operator deliberately
locks down one project/repo at a time. True default-deny-for-every-unconfigured-scope is a
legitimate, real, and stronger posture, but only as an explicitly-named later mode
(`portunus roles lockdown on`, Story 04's closeout doc flags it as a real follow-up, not built
here) — making it the v1 default would functionally lock every agent out of the entire vault on
day one, the opposite of this project's own "byte-identical when inert, staged activation later"
discipline (already proven once for `roles.py` itself).

## 6. Staged rollout, matching this project's own precedent

1. **Story 01 (plumbing):** thread `Identity.from_env()` through every real
   `check_injectable()` caller. Zero behavior change, provable by the existing test suite passing
   unmodified plus new tests asserting identity is now visible at the call site.
2. **Story 02 (schema + audit-only):** `principal` field, `"repo"` scope type, `roles.evaluate()`,
   wired into `check_injectable()` to write `would-allow`/`would-deny` audit lines. **Never
   raises.** Extends the existing byte-identical-when-inert test discipline: policy evaluation
   must never change what a caller receives or whether an exception is raised, only what gets
   logged.
3. **Story 03 (opt-in enforcement):** `portunus roles enforce on|off|status`, a persisted marker
   under `PORTUNUS_HOME` (not a global env var — keeps it properly scoped per `--home`, like
   everything else in this store). When on, `check_injectable` actually raises `NotAuthorized`
   under the permissive-if-unconfigured posture from §5. Default: off.
4. **Story 04 (closeout):** default-on for *newly-initialized* vaults only (never retroactively
   flips an existing vault's setting), docs, version bump, and a live proof against the real
   393-reference vault — configure one real scoped policy, confirm allow/deny both actually
   happen, confirm every pre-existing reference with no configured policy is completely
   unaffected.

## Self-grill

- **What if `PolicyRecord.principal` is set but the requester is unidentifiable (e.g. a future
  caller that still doesn't pass `requester`)?** Treated identically to "no policy configured" —
  §3's `requester is None` branch. This is deliberately fail-open at the identity layer, not
  fail-closed, because the alternative (fail-closed on missing identity) would make this feature
  capable of breaking legitimate flows that were never touched by Story 01's plumbing pass,
  which is a correctness risk this design explicitly wants to avoid introducing silently.
- **Does writing an audit line on every single resolve (Story 02) meaningfully grow the audit
  chain / change its performance characteristics?** One extra `audit.append()` call per resolve,
  same cost class as every other audit line this codebase already writes on every resolve today
  — not a new access pattern, just a new reason string on an existing pattern.
- **Should the approval-token identity gap (Tier 2, research-brief.md §4 — `Broker.approve()`
  tokens scoped to a reference name only, not the requesting identity) be fixed here?** No —
  named explicitly as a real, separate finding and deliberately deferred to a follow-up epic so
  it doesn't expand this one's estimate (research-brief.md §7). Noting it here so it isn't lost.
- **Should the existing roles Settings UI be relabeled to make clear policies are inert until
  Story 03 ships?** Yes — cheap, and research-brief.md §4 (Tier 2) calls this out as a real,
  independent risk today (a user can configure policy records and reasonably believe they're
  already enforced). Folded into Story 02 as a one-line UI label change, not a separate story.
- **What about the `list`/`tree` MCP tools returning full-vault metadata regardless of caller
  scope (Tier 2)?** Real, but explicitly out of scope for this epic (research-brief.md §7) —
  enforcement here only gates the actual `resolve`/inject path, not read-only metadata
  enumeration. A natural Story 05 for a follow-up epic once enforcement itself is proven.

## Open Questions

1. Most-specific-wins scope precedence (§3) is deferred rather than built — confirm this is the
   right v1 call, or whether a narrowing model should ship now instead of later.
2. Story 03's `portunus roles enforce` — confirm the command name/shape before Story 03 starts;
   `on|off|status` mirrors `portunus vault status`-style existing CLI conventions but hasn't been
   checked against every other `roles` subcommand for naming consistency.

## Scale assessment

Medium: touches `broker.py` (new guard clause + exception), `roles.py` (schema field + new
scope type + new evaluator function + enforcement-flag persistence), `resolver.py`/`cli.py`/
`mcp_server.py`/`leakscan.py` (plumbing only, Story 01), CLI (`--principal` flag, `roles enforce`
subcommand), and the Settings UI (principal field + inert-until-enforced label). No changes to
`LocalEncryptedBackend`, `AuditChain`'s hash-chain mechanics, or any vault-routing/backend logic.
`version_bump: minor` — additive, backward-compatible for every existing `roles.json`/vault by
construction (§2, §5).
