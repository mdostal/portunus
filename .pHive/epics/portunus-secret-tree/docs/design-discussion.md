# Design Discussion: portunus-secret-tree

## 1. What Are We Doing?

Giving Portunus's metadata real structure so an agent can navigate *relationships* between
secrets, not just read a flat description per secret. Concretely: `group` (a hierarchical path
placing a secret in a tree, e.g. `project-y/supabase/auth`) and `related` (explicit
cross-references to other secrets by name, e.g. the Supabase auth key naming the MongoDB key it
sits next to) — plus `portunus tree`, the LLM-facing query surface, and a UI tree rendering.

"Done" means: a human can organize secrets into a real hierarchy and link related ones with a
note about how they fit together (auth vs. database, dev vs. personal-test vs. staging), and an
agent can call `portunus tree --project X --json` (or browse the UI's Project Explorer) and get
back that whole structure — never a value — to reason about which key to inject for a task.

## 2. What I Found

See `docs/research-brief.md`. The schema question was resolved via AskUserQuestion before this
document was written: structured `group: str` + `related: List[str]` fields, not a tags
convention and not a single free-text prose field — the user picked the option that "matches
what you described most literally" (a real tree + explicit links), accepting the larger schema
footprint over the cheaper alternatives.

Every extension point needed already exists and has been used twice before for exactly this
shape of change (`Registry.add()`/`retag()`'s additive-kwarg pattern, `DetailDrawer`'s Move
form as the single edit surface, `--json` as the UI/LLM output convention). The only genuinely
new piece is tree-building itself (flat list -> nested structure), needed in two places
(Python CLI, TypeScript UI) with no shared runtime between them.

## 3. My Proposed Approach

**Slice A — Registry schema.** Add `group: str = ""` and `related: List[str] = field(default_
factory=list)` to `Reference`. Neither joins `_STRUCTURED_TAG_FIELDS` (informational, not
tag-matchable identity — same precedent as description/purpose). `Registry.add()`/`retag()`
gain `group`/`related` kwargs, additive, no collision check (same reasoning as description/
purpose: these can never collide because they're not part of tag-matching).

**Slice B — CLI write support.** `--group`/`--related` on `reg add`, `drop`, `retag`. `related`
is list-shaped (not dict-shaped like tags/injected_as), so it gets a small sibling parser,
`_parse_related()`: `"name1,name2"` -> `["name1", "name2"]` (trim, drop empties) — deliberately
not reusing `_parse_tags()`, which assumes `k=v` pairs and would reject a bare name.

**Slice C — `portunus tree` command.** `portunus tree [--project X] [--json]`. Fetches
references (all, or filtered to one project), splits each non-empty `group` on `/`, builds a
nested dict keyed by path segment, and renders either an ASCII tree (default) or nested JSON
(`--json`, the UI/LLM format). Each leaf shows the reference name + a `related` line when
non-empty; a `related` entry naming a reference that isn't in the current result set is marked
`(unresolved)` rather than silently dropped or hard-failed — metadata consistency is
informational, not a fail-closed boundary concern. *(Grill H1)* Every reference with an empty
`group` renders under an `(ungrouped)` bucket at the root, listed flat — never silently
dropped. This is not hypothetical: 382 real references exist in the vault right now (registered
live via `portunus-gcp-multi-account`'s discovery this session), none with a `group` set — the
tree command has to handle that as the common case on day one, not an edge case.

**Slice D — UI plumbing.** `group`/`related` added to both `PortunusReference` copies and to
`/api/retag`'s forwarded fields (mirroring Slice B's flags exactly, `related` joined/split as
a comma string the same way `injected_as` uses the `k=v,k2=v2` string convention today).

**Slice E — DetailDrawer display + edit.** The metadata block gains `group`/`related` lines
(show-only-if-set, same as description/purpose/injected_as); the Move form gains `group` (text
input) and `related` (comma-separated names input) fields, same one-form-one-handler pattern
used for every prior metadata extension.

**Slice F — Project Explorer tree view.** The "Registered" list always renders as a nested tree
now (replacing the flat list) — grouped references nest by `group` path, ungrouped ones render
under an `(ungrouped)` node at the root *(Grill H1 — same resolution as Slice C, one code path:
an all-ungrouped project is just the degenerate case of a tree that's entirely one bucket, not
a separate flat-vs-tree special case)*. Built client-side from the same `/api/list` data already
fetched — no new route. `related` names render as small inline chips; a chip for
an unresolved name (not in the current project's result set) is visually distinct
(`related (unresolved)`), matching Slice C's CLI behavior.

**Slice G — Closeout.** Full pytest + `npm run build` + a live smoke test building a real
2-3-level tree with a cross-project `related` link, README/CONTEXT.md updates, version bump,
CHANGELOG.

## 4. What Could Go Wrong

- **[medium] Two independent tree-building implementations (Python CLI, TypeScript UI) could
  drift in behavior** (e.g. one handles a leading/trailing `/` differently). Mitigation: both
  implementations documented against the same normalization rule (trim, split on `/`, drop
  empty segments) and each gets its own explicit tests for that rule — not shared code, but a
  shared, written-down contract.
- **[medium] `related` naming a nonexistent reference could look like a bug rather than a
  legitimate forward-declaration.** Mitigation: explicit `(unresolved)` marking in both the CLI
  tree output and the UI chip, rather than silently hiding or erroring — visible, not silently
  wrong, not fail-closed-blocking (§3 Slice C).
- **[low] A very deep or wide `group` hierarchy could make the UI tree unwieldy.** Out of scope
  for this pass — no collapse/expand state, no depth limit; revisit if real usage shows it's a
  problem (mirrors the same "don't build for hypothetical scale" call made for discovery's
  unpaginated list in portunus-vault-metadata-ui).
- **[low] `group` paths and `project` (the existing structured field) could seem redundant.**
  Mitigation: state explicitly in CONTEXT.md that `project` is identity/tag-matchable (used by
  `resolve_by_tags`/`list_by_project`), while `group` is purely organizational placement within
  that project (or across projects) — different jobs, not a replacement for one another.

## 5. Dependencies and Constraints

- Slice A is a hard prerequisite for everything else.
- Slice B depends on A; Slice C depends on A (reads `group`/`related` directly from Registry).
- Slice D depends on A/B (the CLI flags it forwards to must exist).
- Slice E depends on D; Slice F depends on D (both need the UI types/route in place).
- Slice G runs last.
- `secret-boundary-invariant`/`audit-chain-integrity` apply throughout — no new value-adjacent
  surface is introduced anywhere in this epic.

## 6. Open Questions

1. Should `portunus tree` (no `--project`) show every reference across all projects in one
   tree, or require `--project`? *(My call: support both — `--project` filters first, then
   builds the tree from whatever's left; omitting it builds one tree across the whole registry.
   Matches `list`'s own `--project`-required-but-`tree`-can-go-broader distinction naturally,
   since a cross-project tree is a reasonable "show me everything" query an agent might ask.)*

## 7. Verification Strategy

```
VERIFICATION PLAN:
  Tools: pytest, npm run build, manual/Playwright smoke test
  Automated: group/related round-trip through add/drop/retag; group excluded from
    _STRUCTURED_TAG_FIELDS (no collision check); portunus tree's path-splitting/nesting logic
    (multi-level, unresolved-related marking, --json shape); CLI/UI type parity for the two
    new fields.
  Manual: build a real 2-3-level group tree via the UI's DetailDrawer edit form across a
    handful of demo references, including one related link crossing groups, and confirm the
    Project Explorer tree renders it correctly with the unresolved-marking case exercised too.
  Not verifying: any settings-page/backend-mode work (explicitly a separate, later epic).
```

## 8. Scale Assessment

```
SCALE ASSESSMENT:
  Files affected: ~10 (registry.py, cli.py + tests; 2 type files; retag route; DetailDrawer;
    ProjectExplorer; README/CONTEXT.md/CHANGELOG/version files)
  Subsystems: Registry (schema), CLI (new command + flags), UI (display/edit/tree render)
  Migration required: no (additive fields, default ""/[])
  Cross-team coordination: no
  Unknowns: 1 (Open Question 1), low-stakes, already defaulted above

  RECOMMENDATION: Proceed to stories (skip H/V) -- every slice reuses an already-proven
    extension pattern (additive Registry kwargs, the single Move-form edit surface, --json
    output convention); the only new algorithm (tree-building from a flat list) is small and
    well-specified.
  RATIONALE: Medium scope by file count, low architectural risk -- no new subsystem, no new
    edit surface, no new API route for the UI half.
```
