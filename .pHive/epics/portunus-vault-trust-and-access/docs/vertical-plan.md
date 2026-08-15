# Vertical Planning — Slice Plan: portunus-vault-trust-and-access

Revised after user course-correction (2026-08-15) — see horizontal-plan.md's own revision note
for the full context. Slice count grew from 8 to 10; RBAC enforcement dropped entirely (schema-
only stub); an org-hierarchy foundation and custom-views slices were added as the now-explicit
top priority.

## 1. Slicing Strategy

```
STRATEGY:
  Total horizontal items: ~24
  Planned slices: 10
  First slice goal: the org field + the repo/source_files plumbing fix -- both foundational,
    additive, zero behavior change for anyone not using them yet.
  Final slice goal: About/Help page documents every surface this epic built; CONTEXT.md
    updated; full audit-chain verify passes.

  Slicing rationale, three chains now:
    Chain A (foundation): org field -> repo/source_files fix -> completeness signal. Nothing
      else can build a real "sub-vault feel" without the org field existing first.
    Chain B (organization, the user's stated #1 priority): sub-vault navigation (needs Chain
      A's org field) -> custom views (independent of org/project/env, but sequenced after so
      the UI can offer both browsing modes side by side rather than shipping views into a UI
      that doesn't have the structural tree yet).
    Chain C (stubbed governance + onboarding): role/policy schema (stub, no enforcement) ->
      LLM-suggest/human-confirm metadata workflow (does NOT need real RBAC -- an unenforced
      vault lets any human confirm, matching the "additive, not breaking" precedent) ->
      Settings page (surfaces bindings + hierarchy config + the greyed roles stub) -> setup
      wizard (reuses Settings' backend-picker + roles-stub UI) -> About/Help (documents
      everything, written last on purpose).

  Checkpoint note: Chain A+B (Slices 1-4) directly answers the user's stated #1 priority
  ("ensure we get the metadata, the sorting, and can tag keys to where they go... the map is
  unmanageable at 30+ repos") and is real, shippable value on its own -- flagged as the
  strongest checkpoint candidate if this epic runs long across sessions. Chain C is real but
  explicitly lower urgency per the user's own "roles part can be stubbed" framing.

  Explicitly NOT in this epic: RBAC enforcement (schema only, per Chain C); multi-instance
  compliance isolation (already solved by --home, not new work); Infisical/other stub backends
  becoming real (still stubs, wizard just PRESENTS them consistently with ProjectExplorer's
  existing two-zone picker).
```

## 2. Vertical Slice Plan

### Slice 1: Org field + repo/source_files UI plumbing fix

**BUILDS ON:** nothing
**WHAT WORKS AFTER THIS SLICE:** `Reference` has a new `org` field (same flat-tag pattern as
`project`/`env`; absent-org references land in a "(no org set)" bucket, never dropped — same
precedent `tree --by`'s ungrouped bucket already set). `reg add`/`retag`/`retag-bulk` accept
`--org`. `/api/retag` forwards `repo`/`source_files`; both UI edit forms gain the missing
inputs — the exact field pair the user named first is now UI-editable.
**LAYERS TOUCHED:** Registry, CLI, UI (`/api/retag`, `DetailDrawer.tsx`, `AddSecretForm.tsx`).
**NOT YET:** no UI surfaces `org` yet (that's Slice 3) — this slice is schema + CLI + the one
concrete bug fix.
**VERIFIED BY:** pytest (org field round-trips, absent-org never breaks existing tree/list
calls); `npm run build`; live Playwright for the repo/source_files fix.
**SHIP CHECKPOINT CANDIDATE:** yes.

---

### Slice 2: Metadata completeness signal

**BUILDS ON:** Slice 1 (shares form real estate)
**WHAT WORKS AFTER THIS SLICE:** unchanged from the original plan — a derived, not-stored
completeness indicator (missing description/purpose/project/tags) on every rendered reference.
**LAYERS TOUCHED:** UI.
**VERIFIED BY:** live Playwright against fixture references at varying fill levels.
**SHIP CHECKPOINT CANDIDATE:** yes, with Slice 1.

---

### Slice 3: Sub-vault navigation (org → project → env drill-down)

**BUILDS ON:** Slice 1 (needs the `org` field to exist)
**WHAT WORKS AFTER THIS SLICE:** a user can drill from "all orgs" into an org (e.g. "Firefly
Events"), then a project (`shindig`), then an env, and each level renders as a scoped,
filtered view — its own reference list, its own completeness summary (Slice 2's signal,
aggregated) — directly answering *"the map is unmanageable at 30+ repos."* Replaces/extends
`VaultMap.tsx`'s current flat render.
**LAYERS TOUCHED:** UI (`VaultMap.tsx`, new drill-down navigation), API (aggregation, likely
computed from existing `/api/list`/`/api/registry` data — no new backend query needed).
**NOT YET:** no permission boundary at any level (Slice 5 is schema-only, unenforced) — this
is purely a navigation/scoping UX improvement.
**VERIFIED BY:** live Playwright against a fixture vault with several orgs/projects/envs —
confirm each drill-down level shows only its own scope's references.
**SHIP CHECKPOINT CANDIDATE:** yes, with Slices 1-2 — this is the strongest single deliverable
for the user's stated #1 priority.

---

### Slice 4: Custom views/collections

**BUILDS ON:** nothing structurally (independent of org/project/env), sequenced after Slice 3
so the UI can offer both browsing modes together.
**WHAT WORKS AFTER THIS SLICE:** a user can create a named collection (`views.json`), add/
remove references to it (from Console, ProjectExplorer, or the new sub-vault views), and
switch the UI into "my views" browsing mode alongside the structural org/project/env tree —
directly answers *"a custom view where I cluster the keys how I want as I prep them for a
project."*
**LAYERS TOUCHED:** new store (`views.json` + lock), CLI (`portunus views ...`), UI (create/
edit/switch UI).
**VERIFIED BY:** pytest (CRUD + concurrent-write lock test, same discipline as every prior
locked-store story this session); live Playwright (create a view, add references from two
different structural locations, confirm it renders correctly).
**SHIP CHECKPOINT CANDIDATE:** yes, with Slices 1-3.

---

### Slice 5: Role/policy schema (stub only — no enforcement)

**BUILDS ON:** nothing (independent of Slices 1-4)
**WHAT WORKS AFTER THIS SLICE:** `roles.json` (+ lock) exists; holds `{scope_type: org|
project|env, scope_value, role, actions[]}` records; `portunus roles set/show` works for real
(writes genuinely persist) — but `check_injectable`/`retag()` never read it. A present, visible,
inert seam, exactly like `Identity.requester` already is today for secret access.
**LAYERS TOUCHED:** new store, CLI, Audit (`roles_config_changed` — a config-change record, not
a grant, since nothing enforces it yet).
**NOT YET:** enforcement (a future epic's job — Petitio's, explicitly deferred by the user).
**VERIFIED BY:** pytest (CRUD + lock test); explicit test asserting `retag()`/
`check_injectable()` behavior is BYTE-IDENTICAL whether or not `roles.json` exists (proves the
stub is truly inert, not accidentally half-wired).
**SHIP CHECKPOINT CANDIDATE:** weak alone (no visible UI yet — that's Slices 7-8).

---

### Slice 6: LLM-suggests / human-confirms metadata workflow

**BUILDS ON:** Slice 2 (completeness signal motivates a suggestion)
**WHAT WORKS AFTER THIS SLICE:** unchanged from the original plan — `portunus_suggest_metadata`
MCP tool writes to a provenance sidecar; UI shows "suggested by an agent, confirm?"; accept
applies via a real human-attributed `retag()`, reject discards. Does NOT depend on Slice 5 —
an unenforced vault lets any human confirm, matching this epic's own "additive, not gating"
discipline throughout.
**LAYERS TOUCHED:** Registry (provenance sidecar), MCP server, UI, Audit.
**VERIFIED BY:** pytest (AST-level check: the suggest tool never writes the live field
directly); live Playwright accept/reject round-trip.

---

### Slice 7: Settings page

**BUILDS ON:** Slice 5 (roles stub needs a place to show as "coming soon"), absorbs whatever
the prior `portunus-bindings-settings-ui` epic left in Project Explorer at this epic's
discretion.
**WHAT WORKS AFTER THIS SLICE:** a real Settings tab — vault-binding management, org/project/
env hierarchy config, and a clearly-labeled, visibly greyed-out "Roles (coming soon)" section
(never a silently-missing feature).
**LAYERS TOUCHED:** UI (new page), API (roles CRUD route mirroring `/api/bindings`'s shape).
**VERIFIED BY:** `npm run build`; live Playwright.

---

### Slice 8: First-run setup wizard (expanded scope)

**BUILDS ON:** Slice 7 (reuses its backend-picker and roles-stub UI components)
**WHAT WORKS AFTER THIS SLICE:** the full 5-step walkthrough from horizontal-plan.md §3 —
explain Portunus's parts, first-vault setup (backend choice including stubs, explicitly framed
as "vaults are separate, chosen per level"), in-UI GCP auth capture (new — no existing route
does this), a greyed-out roles step with Continue, then straight into discover/sort using
Slices 1-4's completeness/navigation UI. Only shown on a genuinely uninitialized
`PORTUNUS_HOME` (design-discussion.md §5's detection rule, unchanged).
**LAYERS TOUCHED:** UI (new wizard flow), API (new GCP-auth-capture route — the one genuinely
new backend surface in this slice).
**VERIFIED BY:** live Playwright against a fresh scratch `PORTUNUS_HOME` (wizard shows, walks
through, lands in the normal UI) and a populated one (wizard never shows).

---

### Slice 9: About/Help page

**BUILDS ON:** everything (documents orgs/projects/envs, views, suggestions, the roles stub,
the wizard — needs them to exist to describe accurately)
**WHAT WORKS AFTER THIS SLICE:** unchanged from the original plan — ported README content plus
new sections for this epic's own additions, including honest documentation of what's stubbed
vs. real (roles) so users aren't misled about what's actually enforced today.
**LAYERS TOUCHED:** UI only.
**VERIFIED BY:** `npm run build`; manual content review.

---

### Slice 10: Closeout

**BUILDS ON:** all prior slices
**WHAT WORKS AFTER THIS SLICE:** CONTEXT.md updated (org/views/roles-stub vocabulary), full
pytest + `npm run build` green, live end-to-end proof (fresh vault → wizard → first project →
org assigned → sub-vault drill-down works → a custom view created → an agent suggests metadata
→ a human confirms → completeness badge clears → roles.json config round-trips but confirmed
inert), version bump, CHANGELOG, ship.
