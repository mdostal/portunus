# Design Discussion — portunus-vault-trust-and-access

Revised after user course-correction (2026-08-15): the role/permission model below is now
explicitly STUB-ONLY for this epic — schema, storage, and a CLI/UI config surface exist and
genuinely persist, but nothing in `check_injectable`/`retag()` ever reads it. Real enforcement
is deferred to a future epic (Petitio's own future, per the user: "the roles part can be
stubbed, but is part of petito and is deferred"). New §0 covers the hierarchy (`org` field,
sub-vault navigation) the user clarified is the actual near-term priority.

## 0. The hierarchy: `org` → `project` → `env` maps onto what "vault" already mostly means

The user's mental model of "vault" turns out to map closely onto what already exists:
`VaultBinding` + `project` already give "each vault is its own thing and place" (its own
backend, its own credential — GCP multi-account already proves this works, one project/vault
per binding) and already support both directions the user described — many apps sharing one
project (`ffe-cicd` hosting several apps' secrets under one binding) or one app owning its own
project outright. What's genuinely missing is the level ABOVE project: nothing today groups
several projects under one organizational umbrella (e.g. "Firefly Events" spanning `ffe-cicd`/
`shindig`/`personalsites-487021`), which is exactly the level the user wants to grant broad
("dev access across the entirety of Firefly Events") vs. narrow ("admin of Shindig specifically,
but only prod-release rights there") permissions at, eventually.

**Decision:** add `org: str = ""` to `Reference`, one level above `project`, using the exact
same flat-structured-tag pattern `provider`/`project`/`env` already use — not a new nested
object model. Absent `org` is never an error, same as every other optional structured field —
lands in a "(no org set)" bucket, same non-dropping precedent `tree --by`'s "(ungrouped)"/
"(no repo set)" buckets already established. This is intentionally the SMALLEST change that
makes org→project→env a real, queryable hierarchy: one field, additive, backward-compatible.

**Sub-vault "feel," not a new store.** The user's own framing — *"we just need to feel like it
is and know where it sits"* — is taken literally: sub-vaults are a UI/navigation concept built
entirely on `org`/`project`/`env` (already-existing or newly-added flat fields), not a separate
per-sub-vault database, permission boundary, or backend instance. Drilling into `Firefly Events
→ Shindig` filters to that scope and shows its own completeness/reference-count summary — it
LOOKS and NAVIGATES like its own vault. Whether it eventually GATES like one is Chain C's
(stubbed) job, activated by a future epic, not this one.

**Explicitly out of scope, and why:** the user separately named genuine compliance-grade
isolation (HIPAA etc, "multiple instances that cannot interact... agnostic and ignorant of one
another") — that is NOT what `org`/sub-vault-navigation solves, on purpose. True non-interacting
isolation already exists today via `--home <path>` (a fully independent `PORTUNUS_HOME`
directory — separate registry, separate audit chain, separate everything, zero shared code
path at runtime). Conflating "organize my 30+ repos into a navigable hierarchy within one
instance" with "guarantee two deployments can never see each other's data" would be a real
category error — the former is a UX/query concern, the latter is a deployment-topology concern
already solved.

## 1. The role model: hierarchical scope, `Identity.kind` stays coarse, STUB ONLY this epic

`Identity(name, kind: Literal["human", "agent", "system"])` already exists (broker.py) and
answers *what kind of actor is this*. The user's ask ("human, llm, dev etc," plus the fuller
Shindig example — org-wide dev access, project-level admin, env-scoped prod-release rights) is
really asking for two more, orthogonal questions: *what is this actor allowed to do*, and *at
what scope*. Conflating role with `kind` (e.g. adding `"dev"` as a fourth `kind`) would be
wrong — a "dev" is a human, full stop; what's different is their permission level AND the
org/project/env scope it applies at.

**Decision:** a `PolicyRecord` shape — `{scope_type: "org"|"project"|"env", scope_value: str,
role: str, actions: list[str]}` — persisted in `roles.json`, directly modeling the user's own
example:

```json
[
  {"scope_type": "org", "scope_value": "firefly-events", "role": "dev", "actions": ["read", "test"]},
  {"scope_type": "project", "scope_value": "shindig", "role": "admin", "actions": ["read", "test", "prod-release"]}
]
```

Default role vocabulary (`owner`/`contributor`/`dev`/`viewer` or similar) is intentionally
left OPEN/configurable, not hardcoded — the user's own "admin... dev access... prod releases"
example already shows role meaning is scope-dependent (an org-wide "dev" and a project-level
"admin" aren't the same shape of permission), which argues against baking in one fixed role
enum this early. This epic ships the shape and a CLI/UI surface to populate it; it does **not**
ship the code that evaluates "does this identity's set of policy records grant this action at
this scope" — that evaluation function is real, non-trivial (most-specific-scope-wins? explicit
deny beats allow? — genuine open questions a future epic resolves), and building it now, unused,
would be exactly the kind of speculative work this epic's own org-hierarchy section (§0) argues
against doing for compliance isolation.

**This epic's own retag()/check_injectable() behavior is BYTE-IDENTICAL whether or not
`roles.json` exists or has content** — the defining test of "genuinely inert," not just
"defaults to permissive." This mirrors `Identity.requester`'s own current status for secret
access exactly, extended in SHAPE (not enforcement) to metadata/hierarchy actions.

## 2. Why routing fields (repo/project/env/backend) are human-only, never agent-suggestible

`description`/`purpose`/`tags`/`group` are informational — wrong is annoying, not dangerous.
`repo`/`project`/`env`/`backend` are **routing** — `backend.py`'s own 3-level precedence tree
(§2 of architecture.md) uses `project` to pick which credential/backend a value resolves
through. An agent that could set `project` on a reference could effectively redirect where a
future resolve fetches from. This is the same boundary-only discipline `ProjectExplorer`'s own
two-zone real/stub backend picker and `portunus drop`'s harness-side-only value entry already
enforce elsewhere in this codebase — informational fields get the lighter suggest/confirm
workflow; anything with resolution-time consequences stays a direct, human-only `retag`/`bindings
set` call, unchanged by this epic.

## 3. Suggestion provenance shape

**Decision:** a single sidecar dict on `Reference`, `suggested: dict[str, dict]` — e.g.
`{"description": {"value": "...", "by": "claude-code", "at": "2026-08-15T..."}}` — rather than a
separate `PendingSuggestion` record/table. Reasoning: suggestions are per-reference, per-field,
and there's at most one live suggestion per field at a time (a second suggestion for the same
field before the first is resolved simply overwrites the pending one — last-suggested-wins for
the *proposal*, never for the *live* field, which only ever changes on explicit human confirm).
Keeps the schema additive (one new field) and keeps `retag()`'s existing shape as the ONLY path
that ever writes to the real `description`/`purpose`/etc fields — `portunus_suggest_metadata`
(new MCP tool) only ever writes to `suggested{}`, structurally incapable of touching the live
fields (mirrors `discover.py`'s own "structurally cannot call `.access()`" restraint pattern —
the tool's implementation shouldn't even import whatever function would let it write live
fields).

## 4. Confirm/reject is itself a retag() call, not a new mutation path

Accepting a suggestion in the UI calls the SAME `retag()` (with the human's `Identity` as
`requester`) that a manual edit would — it just pre-fills the value from `suggested{}` and
clears that field's sidecar entry on success. No second write path to maintain, no risk of the
confirm flow drifting from the manual-edit flow's own validation/locking/audit behavior.
Rejecting just clears the sidecar entry — no `retag()` call at all (nothing about the live
reference changes).

## 4a. Custom views/collections: manual curation, not a saved query, for v1

The user's example — *"a custom view where I cluster the keys how I want as I prep them for a
project"* — is task-shaped, not ownership-shaped: it doesn't cleanly map to org/project/env
(a Shindig deploy prep list might pull from several projects/orgs at once) or to `related`
(pairwise links, not a named group). **Decision:** the simplest possible v1 — a named,
human-curated list of reference names, `{name, description, ref_names: [str]}`, stored in
`views.json` with its own lock. Explicitly NOT a saved tag-query/smart-view for v1 — a query-
based view is a real, larger feature (needs a query language or a structured filter builder)
that isn't needed to serve the described use case (manually assembling a known set of
references for a specific task), and building it speculatively now would repeat the exact
mistake §0 argues against for compliance isolation: solving a bigger, unconfirmed version of
the problem before the smaller, confirmed one is even shipped.

## 5. First-run wizard detection

**Decision:** absence of BOTH `registry.json` and `vault-bindings.json` under `PORTUNUS_HOME` —
not a new marker file. A vault with either file present has been used before (even a single
`portunus drop`/`bindings set` creates one of these) and must never see the wizard again,
regardless of how empty it looks. A brand-new `PORTUNUS_HOME` (fresh install, fresh machine, or
this session's own scratch-vault-per-test pattern) has neither, unambiguously. Avoids a third
state file whose own presence/absence could itself drift out of sync with what it's supposed to
track.

## 6. Self-grill

- *Does an unenforced `roles.json` presence quietly become a false sense of security once users
  see a Settings UI for it and assume RBAC is "on"?* Real risk, and the single biggest one this
  revision introduces (more so than the original enforced-RBAC plan, ironically — a visible-but-
  fake control is a worse trap than no control). Mitigated structurally, not just by copy: the
  Roles section in Settings/wizard is rendered visibly greyed-out/disabled with an explicit
  "coming soon — not yet enforced" label, never a normal-looking, seemingly-live form. Also
  covered in Slice 9's About/Help content directly.
- *What happens to an agent-suggested field if the reference is deleted/retagged away before a
  human confirms?* The sidecar lives on the `Reference` record itself, so it's deleted with it —
  no orphaned suggestion state to clean up separately.
- *Should `retag-bulk`'s existing bulk path also gain a requester/suggestion mode, so an agent
  could suggest metadata for many references at once (directly serving "the tags can get
  updated... as it is used and requested")?* Deliberately deferred past this epic's own Slice 6 —
  matches `portunus-provenance-graph`'s own precedent of shipping the single-reference/dry-run-
  verified tool first and leaving bulk-apply as an explicit human-confirmed follow-up.
- *Does adding `org` on top of the already-existing `group` (a free-text hierarchical path) and
  `project` create three overlapping ways to organize the same reference?* A real risk of
  redundant concepts. Distinction kept deliberately narrow: `org`/`project`/`env` are
  STRUCTURED, one-value-each, and are what the sub-vault navigation (Slice 3) and the future
  (stubbed) permission scoping key off of. `group` stays free-text, human-authored, arbitrary-
  depth, and UNRELATED to permissions — it's a display/organization convenience (`tree --by
  group`), not a scope. No migration between them; both coexist, each serving what it already
  served, `org` just fills the one real structural gap (nothing groups multiple projects today).
- *Is 10 slices in one epic too large?* Larger than the already-large `portunus-standalone-core`
  precedent (7 slices) — named directly, not glossed over. Slices 1-4 (org field, plumbing fix,
  completeness signal, sub-vault navigation, custom views) are flagged as the strongest
  checkpoint if this needs to ship across more than one session, since they alone fully answer
  the user's stated #1 priority. Slices 5-10 (stub roles, suggestion workflow, settings, wizard,
  help, closeout) are real but explicitly lower-urgency per the user's own "roles part can be
  stubbed" framing — kept in the SAME epic (not pre-split) because Slices 5/7/8 share one design
  (the stub-roles UI treatment) that would risk drifting if planned as separate epics later.

## 7. Scale assessment

**Large** — the H/V process exists precisely for scope this size. Slices 1-2: small, additive,
no new architecture (a field + a UI plumbing fix). Slice 3: medium (pure navigation/derivation,
no new store). Slice 4: medium (new locked store, but simple shape). Slice 5: medium (new
locked store + CLI/UI, deliberately no enforcement logic to write). Slice 6: medium (new MCP
tool + UI, built on Slice 2). Slices 7-8: medium, mostly UI. Slice 9: low. Slice 10: low,
standard closeout. `version_bump: minor` throughout (additive — no existing behavior changes
for a vault that ignores every new field/store, per §0/§1/§5's own backward-compat decisions).
