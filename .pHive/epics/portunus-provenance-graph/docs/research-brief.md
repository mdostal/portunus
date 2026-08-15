# Research Brief — portunus-provenance-graph

## 1. Ask

User: *"ffe-ci-cd has hundreds of tokens and it is hard to separate them based on project and
repo and what they bind to etc -- so we should get another view that helps us see and know the
groupings or do a graph or something of them if possible or a nice tree view as we know the file
and repo it is based with or whatever. We need more metadata and i can help fill that but we may
need more."*

## 2. The real data, checked directly, not assumed

`portunus list --project ffe-cicd --json` — 342 references, 30 distinct `group` values. Every
single one of them:

- `state="requested"` — a `discover --register` placeholder, never reviewed by a human
- `description`, `purpose`, `kind`, `related`, `injected_as` — **all empty**, on all 342
- `tags` — only `{"key": "<raw SM name>"}`, the mechanical discovery output
- `group` — a free-text path like `ffe-cicd/event-api/prod`, `ffe-cicd/social-engine/dev`

`related` (the one existing cross-reference field) is populated on exactly **2** references in
the *entire* vault (a Resend API-key/audience-ID pair) — not a usable substrate for a
relationship graph today, at any scale.

**What this confirms:** the user's complaint is accurate and specific. `group` already captures
a rough service/env hierarchy (`ffe-cicd/<service>/<env>`) and `portunus tree` already renders it
— but there is no structured field for *which git repo* consumes a secret (distinct from the GCP
*project*, which is one shared umbrella (`ffe-cicd`) spanning many repos/services), and *nothing
at all* for *which file* in that repo references it. `group`'s second path segment (`event-api`,
`social-engine`, `monitoring`, ...) almost certainly **is** the repo name in practice, informally
— but it's unstructured free text, not queryable/filterable as its own dimension the way
`provider`/`project`/`env` already are.

## 3. Existing infrastructure this epic builds on, not replaces

- `_build_tree()` (`cli.py`) already builds a generic nested tree from any reference set, keyed
  on `group.split("/")` — `portunus tree`, `portunus_tree` (MCP), and Project Explorer's
  client-side `buildTree()` (`ProjectExplorer.tsx`) all consume the identical shape. Trivially
  parameterizable to key on a different field.
- `_STRUCTURED_TAG_FIELDS = ("provider", "project", "env", "scope", "kind")` (`registry.py`) is
  the exact, established pattern for "a field queryable via `find --tags X=Y` and checked for
  fail-closed collision on `retag`" — the right precedent for a new `repo` field, not a one-off.
- `Registry.retag()` already does collision-safe, only-passed-fields-change updates for **one**
  reference at a time. Backfilling `repo` across 342 already-grouped references one `retag` call
  at a time is real, tedious toil — a bulk-by-selector operation is the practical unlock, not a
  new concept.

## 4. What NOT to build this epic

- **A force-directed relationship graph.** `related` has 2 real data points in the whole vault.
  Building a graph renderer to visualize that would be visualizing almost nothing — the honest
  move is to make `related` *easier to see and populate* (already possible via `retag`/the UI's
  edit form) and defer an actual graph visualization until there's real relationship data to
  justify one. Recorded here so it isn't silently dropped, not built now.
- **Auto-discovering which file references a secret** (grepping the user's other 50 repos for
  `{{secret:NAME}}` usage). A real, separate, much bigger feature with real privacy/access
  questions (Portunus would need read access to arbitrary other repos) — not what was asked.
  `source_files` ships as free-text, human-filled metadata, same posture as `description`/
  `purpose` today.
- **Auto-applying a bulk `repo` backfill to the real 342 ffe-cicd entries** as part of this
  epic's own "live proof." The `group`-second-segment-as-repo heuristic is a reasonable
  *default*, but it's a guess about real production infrastructure this session doesn't have
  independent confirmation of — mass-editing 342 real references on that guess, unconfirmed, is
  exactly the kind of consequential action that should be offered, not just done. The bulk tool
  ships this epic; running it against the real data is a follow-up the user can confirm or
  correct field-by-field.

## 5. Scope for this epic

**In scope:** `repo` (new structured tag field, `find --tags repo=...` works immediately) +
`source_files` (new list field, same shape as `related`) on `Reference`; a bulk-retag-by-prefix
CLI command so backfilling many references sharing a `group` prefix is one command, not N;
`portunus tree --by {group,repo}` (CLI + MCP); a Group/Repo toggle on Project Explorer's existing
tree view; `related` rendered as clickable chips instead of plain text (cheap, real UX win, uses
what already exists rather than building a new visualization).

**Out of scope, explicitly deferred:** a real relationship graph visualization (§4); automated
file-reference discovery (§4); running the bulk backfill against real ffe-cicd data
unsupervised (§4, offered as a follow-up instead).
