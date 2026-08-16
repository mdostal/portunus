# Petitio Synthesis: Per-Agent Access Control for Portunus

*Judging 12 research passes (4 topics × Gemini/Codex/Claude) into one buildable plan.*

---

## PART A — Where the three providers agreed and diverged, topic by topic

### A1. Existing systems landscape
**Agreement (strong, all three):** Casbin (`pycasbin`) is the best-fit *adoptable* engine if Portunus were going to adopt one — pure Python, in-process, zero daemon, Apache 2.0, and its policy-row model maps directly onto `PolicyRecord`. Cedar is a credible #2 (genuine in-process Rust-via-PyO3 bindings, and its principal/action/resource/parent-hierarchy shape is a clean semantic match for org→project→env) but loses to Casbin because it requires authoring real `.cedar` policy files — a new DSL — where Casbin can be driven off Portunus's existing plain JSON. All three eliminate, for the same reasons: OPA/Rego (needs a sidecar or WASM runtime — friction disqualifies it for a CLI), the Zanzibar family/SpiceDB/OpenFGA/Keto (best conceptual fit for hierarchical scoping, but all are servers — a hard disqualifier), SPIFFE/SPIRE (solves identity, not authorization, and needs a daemon fleet), Vault's ACL engine (not separable from running Vault, now BUSL-licensed, and adopting it would mean becoming a Vault client — the opposite of what Portunus is), Kubernetes RBAC (not a library, only exists inside `kube-apiserver`), and Teleport/Boundary (full multi-node broker platforms that would *replace* Portunus's own `Broker`, not plug into it).

**Divergence (minor, doesn't affect the verdict):** Gemini and Codex both flag Biscuit (capability tokens) as an interesting #3 for a *future* "hand this agent a scoped, unforgeable grant" flow; Claude ranks OPA-via-subprocess third instead, more skeptical of introducing a bearer-token paradigm that doesn't exist anywhere in Portunus's current lookup-based flow today. This doesn't matter for v1 — see A4, all three independently conclude *none of this should be adopted right now anyway.*

**Judgment:** Treat the landscape survey as answering "what's the fallback if hand-rolling turns out to be wrong" rather than "what to build now." That fallback is Casbin, full stop. File it away; don't build against it yet.

### A2. Portunus-specific integration architecture
**Agreement (strong):**
- Keep `DOSTAL_AGENT`/`Identity.from_env()` as the v1 identity signal. No cryptographic tokens, no per-agent credentials. All three give essentially the same threat-model reasoning: this is a local-OS-process boundary, not a network boundary — a credential stored anywhere the same OS user's agent process can read it is defeated as trivially as the env var is.
- Extend `PolicyRecord` with a principal concept rather than build a second store. `roles.py` stays the single source of truth; the CLI/Settings UI stay the only authoring surface a human ever touches.
- Each `--home` gets its own fully independent policy store — no federation, ever. All three agree federation would directly undermine the one guarantee `--home` exists to provide.
- `check_injectable()` stays the enforcement trigger point; the actual matching/decision logic should be a separate, named function so it stays swappable and testable independent of the lifecycle/approval guards already there.

**Divergence #1 — breaking vs. non-breaking schema change:** Codex proposes splitting into `principal_kind` + `principal_name` fields and explicitly frames this as an *acceptable breaking change* requiring policy recreation, since the store is "explicitly unenforced" today. Gemini and Claude both instead add a single `principal: str = ""` field where empty/`"*"` means "applies to everyone" — fully backward compatible, every existing `roles.json` record stays valid and behaves exactly as before. **Judgment: side with Gemini/Claude.** The project's own proven discipline is "byte-identical when inert" — there is no reason to force a breaking migration on a real, populated 393-reference vault when a default value achieves full compatibility for free. A single flexible string field (`"agent:claude-ffe"` or bare name matching `Identity.name`) is also simpler than two fields for no loss of expressiveness.

**Divergence #2 — default posture when a scope has no policy at all (the highest-stakes call in this whole synthesis):** Gemini's proposed posture is *true default-deny*: "if no matching policy is found, it fails closed." Claude's posture is *permissive-until-configured*: a scope with zero `PolicyRecord`s behaves exactly as today (allow); enforcement only activates for scopes that have at least one record, denying principals not explicitly listed there. Codex is less explicit but leans toward Claude's side operationally (staged audit-only rollout, deny reserved for the flag-flip stage). **Judgment: Claude's posture is correct as the *initial* enforcement default, and Gemini's is a legitimate *later* hardening mode, not the starting point.** With 393 real references and zero populated principal-scoped policies today, true default-deny would functionally lock every agent out of the entire vault the moment enforcement is switched on, forcing a big-bang policy-authoring exercise before the tool is usable at all — exactly the kind of disruptive flip this project's staging discipline exists to avoid. Permissive-until-configured lets the operator lock down one project/repo at a time (start with the new, small one) while everything else keeps working untouched. True default-deny is worth revisiting later as an explicit opt-in "lockdown mode" once policies actually cover what the operator cares about — but conflating the two is the single biggest way this feature could ship in a way that breaks daily work on day one.

**A gap only Claude surfaced, and it changes the plan:** no caller in the codebase today actually passes `requester=` into `check_injectable()` — `resolver.py`, `cli.py`, `mcp_server.py`, and `leakscan.py` all call it bare, per that research pass's direct reading of the source. `Identity.from_env()` is unit-tested but never invoked at a real call site. If accurate, "wire policy into check_injectable" is really *two* independently-shippable changes: (1) thread `Identity.from_env()` through every real call site first, as a pure-plumbing, zero-behavior-change commit, then (2) make `check_injectable` actually consult it. Neither Gemini nor Codex contradicts this, but neither independently confirms it either — **flagged in Part B's confidence section for verification before scoping the first PR.**

### A3. Threat model
**Agreement (strong):** All three land on the same top tier, independently:
1. **Cross-project accidental resolve** (hallucinated/under-specified reference names, or session/context bleed when an agent is repointed at a different project mid-session) — high likelihood, and with 342 of 393 references under `ffe-cicd`, an under-specified query from a small project is statistically likely to land on the big one.
2. **Prompt-injection-driven resolve** — an agent reading untrusted repo content (README, issue, fetched URL) that instructs it to fetch an unrelated project's secret. All three call this out as the *strongest single argument* for real enforcement, because it's structurally invisible to what already exists: audit chain only records after the value is out, and leak-scan never sees it at all when the value goes straight into a subprocess argv/env (as `resolve_exec`-style flows do) without ever touching disk.
3. **Broad enumeration** (`list`/`tree` defaulting to the whole vault's metadata) — real, but lower severity standalone; valuable mainly as a force-multiplier that feeds scenarios 1–2 by putting hundreds of irrelevant reference names into a confused agent's context.

All three also agree, equally strongly, on what this work does **not** solve: a genuinely malicious or compromised local process can trivially spoof `DOSTAL_AGENT`, and no amount of RBAC/ABAC built on a self-reported env var closes that gap — `--home` isolation is the correct existing tool for a *known*-untrusted third-party tool, and this work shouldn't be sold as defeating an adversarial co-resident process. Full vault isolation for regulated data is likewise already solved by `--home` and is not a justification for this feature.

**Divergence (additive, not contradictory):** Codex uniquely names **environment leakage** — `DOSTAL_AGENT` persisting across a terminal session, tmux pane, or long-running shell and getting inherited by the wrong agent process — as a distinct medium-likelihood scenario neither Gemini nor Claude names explicitly. Claude uniquely surfaces, from direct code reading, an **approval-token scoping gap**: `Broker.approve()`/`gate()` write one approval token per reference name, not per requesting identity, so a concurrently-running agent on a different project can walk through an approval window a human intended for a different agent. Codex also uniquely flags that the *existing* roles CLI/Settings UI already creates **false confidence** — a user can configure and see policy records today and reasonably (wrongly) believe they're doing something. Both additions are real, code-grounded, and worth keeping — see Part B.

### A4. Cost-benefit / build vs. adopt
**Agreement (unanimous, no meaningful divergence):** Hand-roll a minimal Python evaluator. Do not adopt Casbin, OPA, Cedar, or anything else for v1. All three independently price the actual matching logic at roughly 20–30 lines against data already loaded by `roles.py`'s existing `load_policies()`. All three point to the same reason this is right for *this* project specifically, not authorization work in general: Portunus has exactly one operator who is also the sole policy author (via a CLI they already have), a handful of agents, and three-to-four scope dimensions — the multi-tenant, multi-author, delegated-administration problem that Casbin/OPA/Cedar are actually built for doesn't exist here. All three also converge on the same rollout shape: inert schema extension → audit-only evaluation (log would-allow/would-deny, never block) → opt-in enforcement flag → default-on later. Estimated cost: zero new runtime dependencies, at most one new small module, roughly 6–10 new tests.

**Worth spotlighting:** Claude's pass is the only one to notice, from reading `registry.py` directly, that `Reference` already carries a `repo` field distinct from `project` — described as "the git repo that consumes this secret" — and that it is currently unused by any policy match and **not** in `roles.py`'s `VALID_SCOPE_TYPES`. This maps directly onto the user's own framing of the problem ("bind the right... repo access to the right agents"), and means v1 should add `"repo"` as a fourth valid `scope_type`, not just `org`/`project`/`env`. This is a concrete, high-value, low-risk addition that both other passes missed simply because neither read that dataclass as closely — **flag for verification, since only one of three independently confirmed it (see confidence section).**

---

## PART B — Final synthesized report

### 1. Executive summary

Build a minimal, hand-rolled, in-process authorization evaluator that activates the two seams Portunus already shipped and proved inert: `Identity` (threaded through `check_injectable`, currently unused) and `roles.py`'s `PolicyRecord` store (persisted, CLI/UI-managed, currently unenforced). Add one field to `PolicyRecord` (`principal`), add `"repo"` as a fourth `scope_type`, write one new matching function, and call it from inside `check_injectable()`. No new dependency, no new store, no new process, no daemon. Ship it in the same staged, inert-first-then-flip discipline this codebase already uses for `roles.py` itself: schema change → audit-only logging → opt-in per-vault enforcement flag, defaulting to *permissive for any scope with no policy configured* so flipping the switch never breaks existing work on day one. Do not adopt Casbin, OPA, Cedar, or any general authorization engine for v1 — the actual problem (a few agents, one operator, three-to-four scope dimensions, the operator is the sole policy author) is smaller than what any of those tools are built to solve. Do not build cryptographic per-agent identity — the real threat this defends against is honest agent mistakes and prompt-injected instructions, not an adversarial co-resident process, and `DOSTAL_AGENT` is proportionate to that. `--home` already solves hard isolation and stays entirely out of this feature's scope.

### 2. Landscape verdict

**No adoption. Hand-roll it.** If a hand-rolled evaluator is later found to be genuinely insufficient (which is unlikely given the actual scope-matching problem here — org/project/env/repo string/glob comparison against a few hundred records), the documented fallback is **Casbin (`pycasbin`)**: pure Python, in-process, zero daemon, and its tabular policy model is the closest existing thing to `PolicyRecord`. Cedar is a credible second choice if Casbin's model proves too loose, at the cost of introducing a real policy DSL. Every other system surveyed — OPA, the Zanzibar family, SPIFFE/SPIRE, Vault's ACL engine, Kubernetes RBAC, Teleport/Boundary, capability tokens (Biscuit/Macaroons), OAuth2/OIDC — is eliminated either by requiring a server/daemon Portunus's shape forbids, or by solving a fundamentally different problem (workload identity, not authorization; hard storage isolation, already solved by `--home`; delegated multi-tenant administration, which doesn't exist here). This verdict is unanimous across all three research providers and is the least contestable finding in the whole pass.

### 3. Integration architecture

**Where the check lives.** `check_injectable()` remains the single chokepoint and gains a third guard clause, after the existing lifecycle and approval-gate checks, in the same position and style as those two:

```python
def check_injectable(self, name, requester=None):
    ref = self.registry.require(name)
    # ...existing lifecycle guard (unchanged)...
    # ...existing approval gate (unchanged)...
    decision = roles.evaluate(self._policies(), requester, ref)
    if not decision.allow:
        self.audit.append("resolve", ref.sm_name, f"policy:{decision.reason}")
        if enforcement_is_on(self.home):
            raise NotAuthorized(f"{ref.sm_name} is out of scope for {requester.name if requester else 'unknown'}")
    return ref
```

The matching/decision function (`roles.evaluate(policies, requester, ref) -> Decision`) belongs in `roles.py` itself, not a brand-new module — this keeps the "zero new files" cost real (per the unanimous Topic 4 estimate) while still keeping the decision logic out of `broker.py`'s guard-clause sequence and independently testable. `NotAuthorized` is a new exception, distinct from `NotInjectable`/`ApprovalRequired`, so callers and error messages can tell "wrong lifecycle state," "needs approval," and "wrong identity/scope" apart.

**Prerequisite plumbing, as its own commit.** Before any of the above can matter, every real call site — `resolver.py`, `cli.py`, `mcp_server.py`, `leakscan.py` — needs to actually pass `requester=Identity.from_env()` into `check_injectable()`. One research pass found, from direct code reading, that none of them do this today; the parameter exists and is tested in isolation but is never threaded through in practice. Ship this as a standalone, zero-behavior-change PR before touching policy logic at all — it's exactly the kind of small, low-risk, separately-committable change this project already stages on its own.

**Identity.** Keep `DOSTAL_AGENT` / `Identity.from_env()`, unchanged. Do not build tokens, API keys, or cryptographic per-agent credentials for v1. The threat this defends against — honest scope confusion and prompt-injected instructions — doesn't require it, and a credential stored anywhere the same OS-user agent process can read is defeated exactly as easily as the current env var. Revisit only if the trust model changes to a genuinely shared/multi-user machine, which is a different, larger problem than this feature.

**Policy authoring.** Add one field to `PolicyRecord`:

```python
@dataclass
class PolicyRecord:
    scope_type: str            # "org" | "project" | "env" | "repo"  (repo is new — see below)
    scope_value: str
    role: str
    actions: List[str] = field(default_factory=list)
    principal: str = ""        # "" / "*" = applies to everyone (back-compat with every existing record)
```

`portunus roles set` gains an optional `--principal` flag; the Settings UI gains a matching field. Every existing `roles.json` record stays valid, unambiguous, and behaves exactly as it does today (wildcard = applies to everyone), so this is a genuinely additive, non-breaking change — no migration tool, no forced policy recreation.

**Add `"repo"` as a fourth `scope_type`.** `Reference` already carries a `repo` field distinct from `project` (per one research pass's direct reading of `registry.py`, described as "the git repo that consumes this secret"), and it's currently absent from `roles.py`'s `VALID_SCOPE_TYPES`. This maps directly onto the user's own stated goal — binding "the right repo access to the right agents" — and costs nothing extra to add now alongside the `principal` field. **Verify this field's exact current shape before implementing** (see §7) since only one of three research passes independently confirmed it.

**Scope precedence — an explicit design decision to nail down before Stage 2 (not before Stage 1):** when a reference could match an org-level policy *and* a project-level policy *and* a repo-scoped policy simultaneously, pick and document a rule — most-specific-wins for `env > project > org`, with `repo` evaluated as an orthogonal fourth dimension alongside them. Write this once in `roles.py`'s module docstring, next to its existing shape rationale, so it isn't rediscovered ad hoc later.

**Interaction with `--home`.** Zero interaction, by design, permanently. Each `--home` already resolves its own `roles.json` under `home()`, the same root as the registry, audit chain, and approvals dir — the policy store and its enforcement flag simply live there too, with no new top-level location. No federation across `--home` instances, ever: federating an identity's permissions across two hard-separated vaults directly undermines the one guarantee `--home` exists to provide, and no described use case needs it. Document "no federation, by design" explicitly rather than leaving it as a silent gap.

**Default posture — the highest-stakes decision in this design.** A scope with **zero** `PolicyRecord`s configured at all behaves exactly as today: allow, unconditionally, regardless of requester. Enforcement only activates *within* a scope that has at least one `PolicyRecord`, denying any requester not explicitly named (or covered by a wildcard) among that scope's records. This is what makes the enforcement flag safe to flip on a live, 393-reference vault with zero pre-existing principal-scoped policies: nothing breaks until the operator deliberately configures a scope, one project or repo at a time. A stricter "deny anything not explicitly granted, even with zero policy configured anywhere" posture is a legitimate *later*, explicitly-named hardening mode (e.g., a distinct `portunus roles lockdown on`) once the operator has actually covered what they care about — it must not be the starting default, or turning this feature on for the first time will functionally lock every agent out of the entire vault.

### 4. Threat model summary (ranked)

**Tier 1 — real prevention needed; nothing existing covers these:**
1. **Prompt-injection-driven out-of-scope resolve.** An agent ingests untrusted repo content (README, issue, fetched page) instructing it to fetch an unrelated project's secret. This is the single strongest justification for building this feature: it requires no prior machine compromise, can reach the full vault, and is invisible to both the audit chain (records only after the fact) and leak-scan (never touches disk when a value goes straight into a subprocess call).
2. **Cross-project accidental resolve** — hallucinated/under-specified reference addressing, or an agent's session context bleeding from one project to another after being repointed mid-session. The everyday, non-adversarial failure mode this whole shared-vault setup makes possible; with 342 of 393 references under `ffe-cicd`, an under-specified query from a small project is statistically likely to land on the big one.
3. **Human mistagging / over-broad grants.** Easy to happen once among hundreds of references; the authoring tools to prevent it (`roles.py` CLI + Settings UI) already exist and are proven inert. This is the scenario that most directly validates finishing what's half-built rather than starting something new.

**Tier 2 — real, fold into the same design, not independently urgent:**
4. **Broad enumeration** (`list`/`tree` returning full-vault metadata regardless of caller) — low harm alone, but cheaply fixed by the same scoping mechanism and a meaningful force-multiplier for Tier 1 scenarios.
5. **Environment leakage** — `DOSTAL_AGENT` inherited by the wrong process across a persistent shell/tmux session, granting the wrong agent's scope by accident.
6. **Concurrent approval-window sharing** — `Broker.approve()`/`gate()` tokens are scoped to a reference name only, not to the identity that requested the approval, so a different concurrently-running agent can walk through the same window. Narrower than the main problem; note it in the design so the fix (scope the approval token to the approving identity too) doesn't get lost, but don't conflate it with the headline case or let it expand v1's estimate.
7. **Existing roles UI creates false confidence** — a real risk today independent of any new work: a user can configure policy records and reasonably believe they're already enforced. Worth a one-line mitigation (label them clearly as inert until Stage 2 ships) regardless of anything else in this plan.

**Tier 3 — already adequately handled; do not build for these:**
8. **Malicious/compromised third-party MCP tool spoofing identity.** No RBAC/ABAC built on a self-reported env var stops a genuinely adversarial co-resident process — it can read whatever the legitimate agent process can read, identically. `--home` is the correct existing tool here: give a known-untrusted client its own isolated vault. Don't oversell this feature as a security boundary against this scenario.
9. **Full isolation for regulated/highly sensitive data** — already solved by `PORTUNUS_HOME`/`--home`. Not a justification for scoping work inside a shared vault.
10. **Secret mishandled after legitimate resolution** (pasted into logs, committed, etc.) — already the job of leak-scan and the audit chain; enforcement only helps when the fetch itself should have been denied, not what happens after a legitimate one.

### 5. v1 scope recommendation — concrete, staged rollout

**Stage 0 (already done, no action):** `Identity` and `PolicyRecord` exist, are inert, and that inertness is proven by an existing test. Nothing to build.

**Stage 0.5 — plumbing, ship first, zero behavior change:** Thread `requester=Identity.from_env()` through every real `check_injectable()` call site (`resolver.py`, `cli.py`, `mcp_server.py`, `leakscan.py`). No policy logic yet. Prerequisite for everything below; verify against current source before scoping (see §7).

**Stage 1 — schema extension + audit-only evaluation, inert:**
- Add `principal: str = ""` to `PolicyRecord`; add `"repo"` to `VALID_SCOPE_TYPES`.
- Add `roles.evaluate(policies, requester, ref) -> Decision`.
- Wire `check_injectable()` to call it and write `would-allow`/`would-deny` to the audit chain whenever `requester` is present and at least one `PolicyRecord` matches the reference's scope. **Never raise.** Behavior stays byte-identical for every caller — extend, don't break, the existing "stub, not enforced" test discipline (the test only needs to additionally assert no exception is ever raised by policy evaluation).
- CLI/Settings UI gain the `--principal` field.
- Run this against real daily usage for a real stretch. This is the step that reveals whether the scope-precedence rule (§3) actually matches how the real 393 references are organized, before it can lock anyone out.

**Stage 2 — opt-in, per-vault enforcement flag:**
- A persisted marker under `PORTUNUS_HOME` (e.g. `portunus roles enforce on|off|status`), not a global env var — this keeps enforcement state properly scoped per `--home`, consistent with how everything else in this store already works.
- When on: `check_injectable` raises `NotAuthorized` under the permissive-if-unconfigured posture from §3 — a scope with any configured `PolicyRecord` denies unlisted principals; a scope with none stays exactly as before.
- Default: **off.** Adoptable scope-by-scope/repo-by-repo, never a forced big-bang migration.

**Stage 3 — default-on for newly-initialized vaults only:**
- Once Stage 2 has run against the real vault for a real stretch without a false-positive lockout, flip the default so a *freshly created* `PORTUNUS_HOME` starts with enforcement on. Existing vaults are never silently switched — that would be the first breaking upgrade behavior in a codebase that currently has none. An existing vault only enforces if the operator explicitly turned it on in Stage 2.

**Stage 4 (later, explicitly separate, not v1):** a stricter opt-in "lockdown" posture that denies any scope with zero configured policy, for operators who've actually finished covering everything they care about. Also revisit MCP-session identity hygiene (require an explicit agent identity rather than silently falling back to `USER`) once the above is stable.

### 6. Explicitly out of scope for v1, and why

- **Any adopted policy engine (Casbin, OPA, Cedar, or otherwise).** The actual problem — a handful of agents, one operator/policy-author, three-to-four scope dimensions — is smaller than what any of these are built to solve; adopting one now would be carrying a dependency forever to replace a ~30-line function. Revisit only if the hand-rolled evaluator's matching logic genuinely outgrows what a `for` loop over `roles.json` can express.
- **Cryptographic per-agent identity / tokens / API keys.** Doesn't raise the actual security bar against the threat this feature targets (honest mistakes, prompt injection), only against an adversarial co-resident process — which `--home` already handles better by giving that process its own isolated vault instead.
- **Cross-`--home` policy federation, in any form.** Directly undermines `--home`'s reason for existing. If one agent legitimately needs access under two hard-separated vaults, it's invoked twice against two separate `--home` targets, exactly as today.
- **A second policy store separate from `roles.json`.** Would create two files that must agree to produce one decision, double the CLI/UI surface, and double the test matrix, for a fact (`principal`) that's a missing field on an existing record, not a new kind of fact.
- **True default-deny-for-all-unconfigured-scopes as the *initial* posture.** Would functionally lock out every agent from the entire vault the moment enforcement is switched on, given zero pre-existing principal-scoped policies today — the opposite of this project's staging discipline. A legitimate later hardening mode, not a v1 default.
- **Scoping the `list`/`tree` MCP tools and the approval-token identity gap in the same PR as the main enforcement work.** Both are real (Tier 2, §4) and worth designing alongside this feature so they aren't forgotten, but neither should expand the core v1 estimate or block shipping it.

### 7. Confidence flags — verify before committing

- **The "no caller passes `requester=` today" claim.** Central to sequencing Stage 0.5 as its own prerequisite commit; sourced from one research pass's direct reading of `resolver.py`, `cli.py`, `mcp_server.py`, `leakscan.py`, not independently corroborated by the other two. Re-verify against current source before scoping the first PR — if it's stale or only partially true, Stage 0.5's shape changes.
- **The `Reference.repo` field.** Only one of three research passes read `registry.py` closely enough to surface it, describing it as distinct from `project` and currently unused by any policy match, absent from `VALID_SCOPE_TYPES`. This is the detail that most directly operationalizes the user's own "repo access" framing, so it's worth keeping — but confirm its exact current field name/shape in code before writing the `scope_type` change, since it's uncorroborated by the other two passes.
- **MCP server process/identity architecture.** All three passes assume the MCP server is effectively spawned per-agent (inheriting that agent's `DOSTAL_AGENT` env var), which is what makes env-var-based identity workable at all. None of the twelve answers explicitly confirms this from `mcp_server.py`'s actual process/connection model. If the MCP server is instead a single long-lived process serving multiple concurrent client connections, reading identity from *process* env doesn't distinguish between simultaneously-connected agents at all, and identity would need to be established per-session/per-tool-call instead — a materially different (and harder) Stage 0.5. Confirm this before writing any plumbing code.
- **Scope precedence rule.** Flagged in §3 as an explicit decision to write down (most-specific-wins, `repo` orthogonal) — none of the twelve raw answers actually pins this down with full rigor for the four-dimension case (`org`/`project`/`env`/`repo` all present at once); treat the rule in this report as a reasonable default, not a settled spec, and confirm it covers every real combination in the 393-reference vault before Stage 2 ships.