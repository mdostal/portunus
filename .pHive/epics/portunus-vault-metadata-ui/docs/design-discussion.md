# Design Discussion: portunus-vault-metadata-ui

## 1. What Are We Doing?

Surfacing everything `portunus-vault-metadata` shipped (v0.7.0) in the standalone UI, since
the UI is the user's actual UAT surface and none of it is visible there today: description/
purpose/injected_as metadata, GCP secret discovery, the list-by-project browse query, and
whether a project has a GCP WIF binding configured. Explicitly scoped to this repo only — no
`pantheon-v2` or other-repo work, per the user's direct instruction.

"Done" means: a human can see and edit a secret's description/purpose/injected_as in the UI,
open a project-scoped panel that lists what's already registered AND previews/registers
what's discoverable in a live GCP project, and see (never touch) whether that project has a
WIF binding — all through the same CLI-shell-out pattern every existing route already uses.

## 2. What I Found

See `docs/research-brief.md`. The one finding that reshapes scope: **the CLI itself can't
write description/purpose/injected_as yet** — `reg add`/`drop` never got the flags, and
`Registry.retag()` doesn't accept them either, even though `Registry.add()` has since v0.7.0.
The only code that has ever set these fields is `discover.py`'s label-seeding. This has to be
fixed first (Slice A) — everything else in this epic builds on it.

Every existing UI write path shells out to the CLI (`runPortunus()`) and never reimplements
gating logic in TypeScript; `DetailDrawer.tsx` already has a working "Move" form/retag pattern
to extend rather than duplicate. No TS unit-test framework exists yet — every prior UI story
this session verified via `npm run build` + a live smoke test, not unit tests; this epic
follows that same convention for TSX stories while keeping full TDD for the Python stories.

## 3. My Proposed Approach

**Slice A — CLI + `Registry.retag()` metadata support.** Add `--description`/`--purpose`/
`--injected-as` flags to `reg add` and `drop`; extend `Registry.retag()` to also accept and
update `description`/`purpose`/`injected_as` (no collision check needed for these — they're
not tag-matchable, so they can't collide by definition, matching Slice A's original
`_STRUCTURED_TAG_FIELDS` exclusion from `portunus-vault-metadata`). Pure prerequisite; no UI
change in this slice.

**Slice B — Client types + write-route parity.** Add `description`/`purpose`/`injected_as` to
both `PortunusReference` definitions (`ui/lib/portunus.ts`, `ui/app/types.ts` — pre-existing
duplication, kept in sync, not refactored away this pass) and to `/api/retag`'s accepted body
fields (mirroring Slice A's new CLI flags exactly). `/api/registry`'s read path needs no
change — `reg json` already emits every field.

**Slice C — Read path: display metadata.** `DetailDrawer` shows description/purpose/
injected_as (when set) below the existing tags row. `Console`'s table and `VaultMap`'s cards
show a description snippet where there's room (Console: a muted sub-line; VaultMap: card
body). Purely additive rendering, no new state.

**Slice D — Write path: edit metadata.** Extend `DetailDrawer`'s existing Move
form/`moveDraft`/`submitMove` with description/purpose/injected_as fields, POSTing to the
same (now Slice-B-widened) `/api/retag`. No second edit surface — one form, one handler, same
collision-free semantics as Slice A. *(Grill H1)* `injected_as` is dict-shaped
(`{env_name: "env:VAR"|"file:path"}`) — it reuses the exact "k=v,k2=v2" comma-separated
convention `tags` already uses in `AddSecretForm.tsx` (input label
`injected_as (env=target,env2=target2)`, e.g. `prod=env:STRIPE_KEY,staging=file:.env.staging`),
parsed by the CLI's existing `_parse_tags()` helper (splits only on the first `=` per pair, so
a colon-containing value like `env:STRIPE_KEY` is unaffected) — no new parser needed anywhere.

**Slice E — Discovery + list API routes.** `/api/discover` (`GET ?project=<id>` for
diff-only, `POST {project}` for `--register`) and `/api/list` (`GET ?project=<id>`) — both
thin `runPortunus()` wrappers around `portunus discover`/`portunus list --json`, exactly
matching every existing route's shape. `--register`'s never-overwrite/state=requested safety
logic stays entirely in the Python CLI; these routes never reimplement it.

**Slice F — Project Explorer panel (new third tab).** A `"project"` tab alongside
Console/Vault Map: a project-id input, a list view (Slice E's `/api/list`) of what's already
registered, a discover view (Slice E's `/api/discover`) with a "Register" button per
not-yet-registered secret, and a one-line "GCP WIF: configured / not configured" indicator for
the entered project (from a small addition to `/api/discover`'s response or a lightweight new
`/api/gcp-bindings` presence-only route — decided in Open Question 1). The WIF audience value
itself is never sent to the browser, matching `portunus auth gcp`'s own restraint.

**Slice G — Closeout.** `npm run build` clean, static-asset copy, live smoke test (screenshot
or curl) of the new tab + edit form against a temp `PORTUNUS_HOME`, CHANGELOG entry, version
bump, README's "Standalone UI" section gains a one-line mention of the Project Explorer tab.

## 4. What Could Go Wrong

- **[high] The Project Explorer's register action must map 1:1 to the CLI's own
  `--register` semantics** (state=requested, never-overwrite, naming-collision skip) — a
  UI-side reimplementation of any part of that would reopen exactly the safety hole
  `portunus-vault-metadata` closed structurally. Mitigation: the `/api/discover` POST route is
  a bare `runPortunus(["discover", "--provider", "gcp", "--project", id, "--register"])` call
  with no client-side interpretation beyond displaying the CLI's own stdout lines.
- **[medium] Exposing a GCP-binding presence indicator could tempt a future change into
  showing the audience value "for convenience."** Mitigation: state the invariant explicitly
  in code comments at the route boundary (mirrors `portunus auth gcp`'s own restraint) so a
  future edit has to consciously override a documented decision, not just add a field.
- **[medium] The two duplicated `PortunusReference` type definitions could drift** (one
  updated, one forgotten) — pre-existing risk, not introduced by this epic, but this epic
  touches both for the first time since the duplication was introduced. Mitigation: Slice B's
  acceptance criteria explicitly check both files.
- **[low] Adding metadata fields to Console's table/VaultMap's cards could crowd already-dense
  layouts.** Mitigation: description shows only when non-empty, truncated/muted, not a new
  mandatory column — same "show only if set" pattern already used for tags/env chips.

## 5. Dependencies and Constraints

- Slice A is a hard prerequisite for B/C/D (nothing to display/edit without CLI write support).
- Slice B is a prerequisite for C and D (types + route must exist before components use them).
- Slice E is independent of A-D — can build in parallel — but F depends on E.
- Slice F is the only story that adds a new tab/panel; everything else extends existing surfaces.
- Slice G runs last.
- `secret-boundary-invariant` / `audit-chain-integrity` apply throughout, with the new "presence
  not value" angle for the GCP-binding indicator (§4 risk above).

## 6. Open Questions

1. Where does the GCP-binding presence check live? *(My call: extend `/api/discover`'s GET
   response with a `wif_configured: boolean` field — `load_gcp_bindings()` is already a cheap,
   local, no-network read, and folding it into the route the Project Explorer panel already
   calls avoids a whole extra route for one boolean.)*
2. Does the discovery panel need pagination/virtualization for large projects (e.g. the real
   `demo-project-483920` has 19 secrets today, could grow)? *(My call: no — render the full
   list, same as the CLI's own unpaginated stdout; revisit only if real usage shows it's a
   problem. Not worth the complexity for a localhost single-user tool.)*

## 7. Verification Strategy

```
VERIFICATION PLAN:
  Tools: pytest (Slice A), npm run build (every UI slice), manual smoke test / Playwright
  Automated: reg add/drop --description/--purpose/--injected-as round-trip; Registry.retag()
    updates these three fields without a collision check (they're non-tag-matchable);
    /api/retag accepts and forwards the new fields; /api/discover's wif_configured field
    reflects load_gcp_bindings() truthfully.
  Manual: full click-through of the new Project Explorer tab (list + discover + register)
    against a temp PORTUNUS_HOME seeded with demo data; metadata edit via DetailDrawer's
    extended Move form; visual check that description doesn't crowd Console/VaultMap.
  Not verifying: real-network discovery against demo-project-483920 in this epic (already
    verified in portunus-vault-metadata; this epic's UI routes are exercised against a mocked/
    seeded local registry + a stubbed gcloud runner, same discipline as the backend epic).
```

## 8. Scale Assessment

```
SCALE ASSESSMENT:
  Files affected: ~12 (cli.py, registry.py + tests; 2 type files; 3 new/changed API routes;
    3-4 changed components; 1 new tab wiring in page.tsx; README/CHANGELOG/version files)
  Subsystems: CLI/Registry (Python), UI API routes, UI components
  Migration required: no (additive CLI flags, additive UI fields/routes)
  Cross-team coordination: no
  Unknowns: 2 (see Open Questions), both low-stakes and already defaulted above

  RECOMMENDATION: Proceed to stories (skip H/V) -- every slice extends an already-proven
    pattern (CLI-shell-out routes, the existing Move/retag form, the existing tab shell) or is
    a small, well-scoped prerequisite (Slice A). No new architecture.
  RATIONALE: Medium scope by file count and two-language span (Python + TSX), but low
    architectural risk -- nothing here invents a new pattern; it's extension work throughout.
```
