# Design Discussion — portunus-provenance-graph

## 1. Schema: two new fields, same posture as every existing one

- **`repo`** (string, default `""`) — joins `_STRUCTURED_TAG_FIELDS`
  (`provider`/`project`/`env`/`scope`/`kind` → now also `repo`). Immediately gets `find --tags
  repo=event-api`, fail-closed collision-checking on `retag`, and a UI facet chip (Project
  Explorer already renders one chip row per structured field — `repo` slots in identically, zero
  new UI plumbing beyond adding it to the existing facet list). Distinct from `project`
  (`demo-cicd` is one GCP project shared by many repos/services) and distinct from `group` (free
  text, human-organized display path, not queryable as its own dimension today).
- **`source_files`** (list of strings, default `[]`) — same shape and posture as `related`
  (optional, additive, human-filled, no fail-closed semantics, not a `_STRUCTURED_TAG_FIELDS`
  member since it's a list not a scalar). E.g. `["docker-compose.prod.yml",
  ".github/workflows/deploy.yml"]`. Rendered in the UI's metadata block exactly where
  `description`/`purpose`/`injected_as` already render.

Both are additive dataclass fields with `""`/`[]` defaults — every existing reference (all 384 in
the real vault) keeps working unchanged, exactly the migration-safety discipline every prior
metadata addition this session used (`description`, `purpose`, `injected_as`, `group`, `related`,
`backend` all shipped this same way).

## 2. Bulk-retag-by-prefix: the actual unlock for 342 entries

`Registry.retag()` changes one reference. Backfilling `repo` across (e.g.) the 91 references
under `demo-cicd/event-api/*` one at a time is the real toil the user is trying to avoid. New:

```bash
portunus retag-bulk --group-prefix demo-cicd/event-api --repo event-api
```

Semantics: select every reference whose `group` starts with `--group-prefix` (a plain string
prefix match on the existing `group` field — no new selector language), apply the same
only-passed-fields-change + collision-check `Registry.retag()` already does, **per reference,
in the same locked transaction pattern** — one reference failing its own collision check reports
that name specifically and continues with the rest (matches `drop-bulk`'s own "one bad entry
doesn't abort the batch" precedent, `cli.py`'s `cmd_drop_bulk`). Reports `{"updated": [names],
"failed": [{"name", "error"}]}`. A `--dry-run` flag lists what *would* change without writing —
important given this mutates potentially dozens of real references at once; the user should be
able to preview before committing, not just trust a description.

## 3. Tree by repo, not just by group

`_build_tree(refs)` currently does `r.group.split("/")`. Refactored to `_build_tree(refs,
key_fn=lambda r: r.group)`, with a second call site passing `key_fn=lambda r: r.repo or
"(no repo set)"` for the repo facet — same function, same output shape, same renderer (CLI text,
`--json`, MCP, and the UI's `buildTree()`) for both facets. `portunus tree --by repo` / `portunus
tree --by group` (default `group`, unchanged behavior when the flag is omitted — zero behavior
change for every existing caller). `portunus_tree(project="", by="group")` mirrors it on the MCP
side. Project Explorer's tree view gains a two-way toggle above the existing tree render, reusing
`buildTree()` unchanged aside from which field it keys on.

## 4. `related` as chips, not a graph — cheap, honest, real value now

Per the research brief, a real graph visualization would currently be rendering 2 data points.
Instead: `DetailDrawer`'s existing plain-text `related` line becomes clickable chips — click a
related reference's name, the drawer switches to show *that* reference. Uses data that already
exists, costs a small UI change, and makes the *existing* mechanism (which is already
retag-editable) actually pleasant to use — which is also what would make a future real graph
worth building once there's enough `related` data to visualize. Not a placeholder for the graph;
a genuine, separate small improvement that also happens to prepare the ground for one.

## 5. Self-grill

- *Does adding `repo` to `_STRUCTURED_TAG_FIELDS` risk a new collision class on existing data?*
  Checked: collision checking in `retag()` only fires when a value is actually being *set* to
  something that collides with another reference's *full* structured-tag tuple — since `repo`
  defaults to `""` on every existing reference, nothing collides until someone actually sets it,
  same as when `scope`/`kind` were originally added.
- *Should `retag-bulk`'s selector be more powerful than a plain prefix match (regex, tag
  queries)?* Deliberately not — a plain prefix match covers the actual real case (`group`
  hierarchy is already a path), and `--dry-run` covers the "did I select the right set" concern
  without needing a query language. More power can be added later if a real need surfaces; it
  hasn't yet.
- *Is `source_files` the right name, versus reusing `injected_as`?* Confirmed distinct on
  purpose: `injected_as` describes where the *resolved value* goes at runtime
  (`{"prod": "env:STRIPE_KEY"}`); `source_files` describes where the *reference/placeholder* is
  *declared* in source (a `docker-compose.yml`, a CI workflow) — different question, both worth
  keeping.

## 6. Scale assessment

**Medium.** Two additive schema fields (low risk, well-trodden pattern), one new bulk-mutation
CLI command (real but bounded risk, mitigated by `--dry-run` + per-entry error isolation), a
tree-key parameterization (mechanical refactor, not a new concept), and one small UI polish item
(`related` chips). No graph-rendering library, no auto-discovery crawler — both explicitly
deferred with reasoning recorded, not silently dropped. Proceeding to story decomposition.
