# Horizontal Planning Scan: portunus-vault-trust-and-access

Revised after user course-correction (2026-08-15): roles/permissions are STUBBED (schema +
CLI config surface only, no enforcement) — Petitio's job, deferred. Priority #1 is metadata
completeness + a real org→project→env hierarchy so 30+ repos stop feeling like one flat list,
plus user-curated ad-hoc "views" for task-specific clustering. The wizard's scope grew
substantially (educational walkthrough, GCP auth capture in-UI, stub-inclusive backend choice).
Multi-instance compliance isolation (HIPAA etc) is explicitly OUT of scope — already solved by
the existing `--home <path>` mechanism (fully independent `PORTUNUS_HOME` directories, zero
shared code path); not conflated with the sub-vault-within-one-instance hierarchy below.

## 1. Layer Inventory

- **Registry (metadata + hierarchy)** — `registry.py`. Metadata fields already exist (no
  change). **New**: an `org` field, one level above the existing `project` — same flat-
  structured-tag pattern `provider`/`project`/`env` already use, not a new nested model.
  ("Vault," in the user's mental model, maps almost exactly to today's `project` +
  `VaultBinding` — one project, one backend/credential config, already supports many apps
  sharing one project (`ffe-cicd`) or one app owning its own. The missing rung is the level
  ABOVE project that groups several projects under one organizational umbrella, e.g. "Firefly
  Events" spanning `ffe-cicd`/`shindig`/`personalsites-487021`.) Also: a metadata provenance
  sidecar (human-set vs. LLM-suggested-unconfirmed), unchanged from the original plan.
- **Broker (Petitio)** — `broker.py`. Holds the already-inert `Identity`/`requester` seam.
  **Revised**: this epic does NOT wire enforcement into `retag()`/`state`/`drop`. It defines
  the schema `check_injectable` will eventually consume (scope: org/project/env × role ×
  action) and leaves it unenforced — matching the user's explicit "stub it, don't build it."
- **Role/policy store (new, stub-only)** — `roles.json`, same shape/lock discipline as
  `vault-bindings.json` (lock from day one). Holds `{scope_type, scope_value, role,
  actions[]}` records. Readable/writable via CLI (`portunus roles set/show`) and visible,
  greyed-out, in the Settings page and setup wizard — but `check_injectable`/`retag()` never
  read it. A real, present, INERT seam, exactly like `Identity.requester` already is for
  secret access.
- **Custom views/collections (new)** — doesn't exist yet. A named, human-curated list of
  reference names (or a saved tag query) for ad-hoc task clustering ("everything for the
  Shindig deploy") — orthogonal to the structural org/project/env hierarchy, which is
  ownership/routing-shaped, not task-shaped.
- **CLI** — `cli.py`. New `--org` on `reg add`/`retag`/`retag-bulk`. New `portunus roles
  set/show` (stub config, no enforcement). New `portunus views` subcommand family for
  collections.
- **MCP server** — `mcp_server.py`. Unchanged from the original plan: a
  `portunus_suggest_metadata` tool writing to the provenance sidecar only, never the live
  field.
- **UI** — `ui/app/`. The biggest layer, several independent pieces:
  1. Fix the live `repo`/`source_files` plumbing gap (unchanged from original plan).
  2. Completeness/quality badge (unchanged).
  3. **Sub-vault navigation** — drill into an org, then a project, then an env, and the view
     filters/scopes as if it were its own small vault (its own reference list, its own
     completeness stats). This is the fix for "the map is a giant flat thing" — built on the
     org/project/env fields, not a new store. `VaultMap.tsx`'s current flat rendering is the
     direct target.
  4. **Custom views UI** — create/name a collection, add/remove references, switch between
     "structural" (org/project/env tree) and "my views" (ad-hoc collections) browsing modes.
  5. Suggested-vs-confirmed metadata affordance (unchanged).
  6. Settings page — vault-binding management (absorbing what the prior epic left in Project
     Explorer, at this epic's discretion), org/project/env hierarchy config, and a visibly
     GREYED-OUT "Roles (coming soon)" section — never a silently-missing feature, always a
     visible, honest "not yet" state.
  7. Setup wizard — substantially bigger, see §3 below.
  8. About/Help page (unchanged — README content as a starting draft).
- **Audit** — `audit.py`. `retag` gains a real audit entry (unaudited today). `roles.json`
  writes get an audit entry (config change, matches `vault-bindings.json`'s own posture — not
  itself a permission GRANT since nothing enforces it yet, just a config-change record).
- **Infra/config** — `PORTUNUS_HOME`. New `roles.json` + lock, new `views.json` + lock (same
  day-one-lock discipline both times). First-run detection: absence of BOTH `registry.json`
  and `vault-bindings.json` — no third marker file.

## 2. Per-Layer Requirements

```
## Layer: Registry (hierarchy + metadata)

SCHEMA CHANGES:
  - Reference gains `org: str = ""` -- one level above `project`, same flat-tag pattern.
    Migration-free: absent org on every existing reference just means "ungrouped at the org
    level," the same non-dropping "(no repo set)"/"(ungrouped)" bucket precedent `tree --by`
    already established.
  - Provenance sidecar `suggested: dict[str, dict]` (unchanged from original plan, §3 of
    design-discussion.md).

DERIVED, NOT STORED:
  - Completeness signal (unchanged).
  - "org summary" (reference count, completeness %, per org/project/env) -- computed on read
    for the sub-vault navigation UI, not persisted (same anti-second-source-of-truth
    reasoning as the completeness signal).

---

## Layer: Broker (Petitio) -- schema only, NOT wired

SCHEMA (present, inert):
  - A PolicyRecord shape (scope_type: org|project|env, scope_value, role, actions[]) that
    check_injectable/retag() COULD read in a future epic -- this epic defines and persists it,
    never consumes it. Exactly the same "accepted everywhere, enforced nowhere" posture
    Identity.requester already has today for secret access, extended (in shape only) to cover
    hierarchy-scoped metadata/state actions.

---

## Layer: Role/policy store (new, stub)

SHAPE:
  - PORTUNUS_HOME/roles.json + roles.lock (flock from day one -- the vault-bindings lesson,
    not retrofitted).
  - CLI: `portunus roles set --scope-type {org,project,env} --scope-value <v> --role <r>
    --actions <a,b,c>` / `portunus roles show`.
  - UI: visible, editable (writes really persist), but literally decorative until a future
    epic wires enforcement -- Settings page and wizard both show it clearly labeled
    "coming soon."

---

## Layer: Custom views/collections (new)

SHAPE:
  - PORTUNUS_HOME/views.json + views.lock. `{name: str, description: str, ref_names: [str]}`
    -- simplest possible v1 (a curated list, not a saved query -- a query-based "smart view"
    is a real future extension, not needed for the "as I prep them for a project" use case
    described, which is manual curation).
  - CLI: `portunus views create/add/remove/show`.

---

## Layer: CLI

CHANGES:
  - `reg add`/`retag`/`retag-bulk` gain `--org`.
  - New `portunus roles set/show` (stub, see above).
  - New `portunus views create/add/remove/show`.

---

## Layer: MCP server

CHANGES:
  - `portunus_suggest_metadata(name, fields)` -- unchanged from original plan (writes to the
    suggested{} sidecar only).

---

## Layer: UI

CHANGES:
  - Fix repo/source_files plumbing (unchanged).
  - Completeness badge (unchanged).
  - Sub-vault navigation: org -> project -> env drill-down replacing/extending VaultMap's flat
    render; each level shows a filtered reference list + derived completeness summary.
  - Custom views: create/curate named collections; a view switcher alongside the structural
    tree.
  - Suggested-vs-confirmed affordance (unchanged).
  - Settings page: binding management + org/project/env config + greyed-out Roles section.
  - Setup wizard (expanded, see §3).
  - About/Help page (unchanged).

---

## Layer: Audit

NEW ENTRY TYPES:
  - retag (unchanged from original plan)
  - metadata_suggested / metadata_confirmed / metadata_rejected (unchanged)
  - roles_config_changed (config write, not a grant -- nothing enforces it yet)
  - view_created / view_modified (collections are configuration, not secret access, but
    matches this codebase's "every mutation gets a metadata-only trail" default)

---

## Layer: Infra/config

CHANGES:
  - roles.json + roles.lock, views.json + views.lock (both locked from day one).
  - First-run detection: absence of registry.json AND vault-bindings.json (unchanged).
```

## 3. Setup wizard — expanded scope

Per the user's explicit walkthrough, in order:

1. **Explain Portunus and its parts** — OSTIARIUS/ARCA/Petitio in plain language, what
   boundary-only injection means, why it's safe (README's "Why it's safe" section is a direct
   source).
2. **Set up your first vault** — choose ARCA backend, explicitly framed as *"each vault can be
   separate and different, and chosen at each level"* (i.e., this is project #1, not the only
   project you'll ever have) — backend choice includes the stub adapters (Infisical, Vault,
   Doppler, 1Password, Azure), same two-zone real/stub treatment `ProjectExplorer`'s picker
   already uses (a stub tile explains itself, never pretends to be selectable-and-safe).
3. **GCP auth capture, in the UI** — if GCP is chosen, walk through `gcloud auth login`/WIF
   setup from the browser rather than requiring a pre-authenticated terminal. New surface: no
   existing route does this today (auth today is CLI-only, `portunus auth login`/`auth gcp`).
4. **Roles for this vault — greyed out, "coming soon," with a Continue button.** Shows the
   stub UI (§2's Settings-page roles section, reused here) but takes no real input.
5. **Discover and sort** — once a backend is live, walk into `discover`/register, landing the
   user in the completeness-badge/sub-vault-navigation UI this epic already built, to start
   filling in metadata for real.

An already-initialized vault (per §5's design-discussion.md detection rule) never sees this.
