# Research Brief — portunus-petitio-rbac

Distilled from a 13-agent research pass (4 topics × Gemini API / Codex CLI / native Claude,
genuinely mixed providers via a proxy-agent pattern, plus one Claude synthesis-and-judge agent
reconciling all 12). Full raw output: `.pHive/research/petitio-rbac-synthesis.md`. Every claim
below that the synthesis flagged as uncorroborated (only 1 of 3 providers surfaced it) was
independently re-verified against the current source tree before this brief was written — see
"Verified against source" at the end of each relevant section.

## 1. The gap, as it exists today

`Broker.check_injectable(name, requester=None)` (`src/portunus/broker.py:72`) is the single
fail-closed chokepoint every resolve/inject path calls before a value is ever fetched. Today it
checks lifecycle state (enabled/locked/dropped/requested) and the approval-gate flag only — it
never looks at `requester` at all.

`Identity(name, kind: "human"|"agent"|"system")` and `Identity.from_env()` (reads `DOSTAL_AGENT`
for agents, `USER` for humans) already exist as a deliberately-built, currently-inert seam for
exactly this work.

`roles.py` already persists `PolicyRecord(scope_type: org|project|env, scope_value, role,
actions)` to `PORTUNUS_HOME/roles.json`, with a real CLI (`portunus roles set/delete/show`) and
Settings UI to manage records. Nothing reads this store at resolve time — a test
(`test_check_injectable_and_retag_are_byte_identical_with_or_without_roles_configured`) proves
this inertness is a maintained invariant, not an oversight.

**Verified against source:** none of the four real call sites — `resolver.py:71`,
`cli.py:1215`, `mcp_server.py:611`, `leakscan.py:134` — pass `requester=` today; all call
`check_injectable(name)` bare. `Identity.from_env()` is unit-tested in isolation but never
invoked at a real call site. This means "wire policy into check_injectable" is really two
independently-shippable changes, not one (see Story 01).

## 2. Real vault shape (grounds the threat model and the scope design)

393 real references today: 342 under `demo-cicd`, 36 under `demo-project-483920`, 10 under
`coin-finder`, plus a handful of others (`mdostal.com`, `shared`, `demo-app`). One developer,
one machine, multiple concurrently-running AI coding agents (Claude Code, Codex, etc.) against
the same shared local vault. `PORTUNUS_HOME`/`--home` already gives full instance-level
isolation for cases needing hard separation — this work does not touch that.

## 3. Landscape verdict (unanimous across all three providers)

Hand-roll a minimal Python evaluator; do not adopt an external engine for v1. Casbin
(`pycasbin`) is the documented fallback if a hand-rolled matcher ever proves insufficient — pure
Python, in-process, zero daemon, and its tabular policy model is the closest existing thing to
`PolicyRecord`. Every distributed option (OPA needs a sidecar or WASM runtime, the Zanzibar
family/SpiceDB/OpenFGA/Keto are servers, SPIFFE/SPIRE needs a daemon fleet, Vault's ACL engine
isn't separable from running Vault, Kubernetes RBAC only exists inside `kube-apiserver`,
Teleport/Boundary are full broker platforms) is eliminated by the same reason: Portunus is a
single Python CLI + MCP server with zero extra infrastructure, and none of those tools can be
embedded in-process. The actual matching problem here — org/project/env/repo string comparison
against a few hundred records, one human policy author — is smaller than what any of those
tools are built to solve.

## 4. Threat model (ranked, unanimous top tier across all three providers)

**Tier 1 — real prevention needed, nothing existing covers these:**
1. Prompt-injection-driven out-of-scope resolve — an agent ingests untrusted repo content
   (README, issue, fetched page) instructing it to fetch an unrelated project's secret. The
   strongest single justification: invisible to both the audit chain (records only after the
   value is out) and leak-scan (never touches disk when a value goes straight into a subprocess
   argv/env).
2. Cross-project accidental resolve — hallucinated/under-specified reference addressing, or
   session context bleed after an agent is repointed at a different project. With 342/393
   references under one project, an under-specified query is statistically likely to land there.
3. Human mistagging / over-broad grants — the authoring tools to prevent it (`roles.py`
   CLI + Settings UI) already exist and are proven inert; this is the scenario that most directly
   validates finishing what's half-built.

**Tier 2 — real, fold into this design, not independently urgent:** broad `list`/`tree`
enumeration returning full-vault metadata; `DOSTAL_AGENT` env leakage across a persistent
shell/tmux session; `Broker.approve()`/`gate()` approval tokens scoped to a reference name only,
not to the requesting identity (a concurrently-running different agent can walk through the same
approval window); the existing roles CLI/Settings UI already creating false confidence today
(a user can configure policy records and reasonably believe they're enforced).

**Tier 3 — already adequately handled, do not build for these:** a genuinely
malicious/compromised co-resident process spoofing `DOSTAL_AGENT` (no self-reported-identity
scheme defeats this; `--home` is the correct tool — give a known-untrusted client its own
isolated vault); full isolation for regulated data (already `--home`'s job); secret mishandling
after a legitimate resolve (already leak-scan's and the audit chain's job).

## 5. Integration architecture (synthesized recommendation)

- **Check location:** a new guard clause inside `check_injectable()`, after the existing
  lifecycle and approval-gate checks, calling a new `roles.evaluate(policies, requester, ref) ->
  Decision` function that lives in `roles.py` itself (not a new module).
- **Identity:** keep `DOSTAL_AGENT`/`Identity.from_env()` unchanged. No cryptographic per-agent
  tokens for v1 — the threat this defends against is honest mistakes and prompt injection, not
  an adversarial co-resident process (which `--home` already handles better).
- **Policy authoring:** extend `PolicyRecord` with `principal: str = ""` (empty/`"*"` = applies
  to everyone — fully backward-compatible with all 393 existing references' implicit policy
  state, i.e. none configured). Do not build a second store.
- **`repo` as a fourth `scope_type`:** `Reference` already carries a `repo` field, distinct from
  `project`, described in-line as "the git repo that consumes this secret" — it's a real,
  already-populated structured tag field (`registry.py:30`, `_STRUCTURED_TAG_FIELDS`), just
  absent from `roles.py`'s `VALID_SCOPE_TYPES = ("org", "project", "env")`. **Verified against
  source** — confirmed present exactly as described; this was the one research finding only 1 of
  3 providers independently surfaced, and it checks out. Adding `"repo"` is a one-line
  `VALID_SCOPE_TYPES` change, not new plumbing, and maps directly onto the user's own framing:
  "bind the right account and right repo access to the right agents."
- **`--home` interaction:** zero, by design, permanently. Each `--home` already resolves its own
  `roles.json`; no cross-`--home` federation, ever — federating would undermine `--home`'s one
  guarantee.
- **Default posture (the highest-stakes call):** a scope with zero `PolicyRecord`s stays exactly
  as it behaves today — allow, unconditionally. Enforcement only activates within a scope that
  has at least one configured policy, denying principals not explicitly named there. True
  default-deny-everywhere would functionally lock every agent out of the entire 393-reference
  vault the moment enforcement is switched on, since zero principal-scoped policies exist today —
  the opposite of this project's own staging discipline. It's a legitimate *later*, explicitly
  opt-in "lockdown" mode, not the v1 default.

**Verified against source (MCP process model):** `mcp_server.py:627`/`FastMCP("portunus")` is a
stdio server, spawned per-client by the MCP host — confirms the assumption that one process
genuinely maps to one agent's `DOSTAL_AGENT`, so env-var identity is workable as designed.

## 6. Cost-benefit (unanimous, no divergence across all three providers)

Estimated actual matching logic: ~20-30 lines against data `roles.py`'s existing
`load_policies()` already loads. Zero new runtime dependencies. At most one new small function.
Roughly 6-10 new tests. Staged rollout (inert schema extension → audit-only evaluation → opt-in
enforcement flag → default-on for new vaults only) mirrors this project's own existing "stub,
not enforced" precedent for `roles.py` itself.

## 7. Explicitly out of scope for this epic

Any adopted policy engine (Casbin/OPA/Cedar/etc.) — revisit only if the hand-rolled matcher
genuinely outgrows a `for` loop. Cryptographic per-agent identity/tokens. Cross-`--home` policy
federation. A second policy store separate from `roles.json`. True default-deny-for-everywhere
as the *initial* posture. Scoping the `list`/`tree` MCP tools and fixing the approval-token
identity gap in the same stories as the core enforcement work — both real (Tier 2 above), noted
for a follow-up epic, not expanding this one's estimate.
