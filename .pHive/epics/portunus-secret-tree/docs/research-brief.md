# Research Brief: portunus-secret-tree

## Requirement

User's own words: "where do we add the metadata and stuff so we can link and put things in a
tree structure and give more information like -- this is dev posthog key for x and this is the
personal test key for x and this is the staging key for project Y to connect to the supabase
but is only used for auth as the database is done through mongodb ... next to that, mongo db
key -- THAT metadata the LLM should be able to dig through and get so that we can figure out
how to find the right keys for injection."

Resolved via AskUserQuestion into a concrete schema direction (structured fields, not tags-only
or prose-only): every `Reference` gains
- `group: str` — a hierarchical path (e.g. `project-y/supabase/auth`) placing it in a tree
- `related: List[str]` — explicit cross-references to other reference *names* (e.g. a Supabase
  auth key naming the MongoDB key it's used alongside)

plus a `portunus tree [--project X] [--json]` CLI command (the LLM-facing query surface) and a
UI tree rendering. Explicitly NOT in scope this pass: the separate "settings page" question
(backend mode: local/remote/synced) the user asked in the same turn -- confirmed as a distinct,
separate follow-up epic, not folded into this one.

## Current state (verified against code)

- `Reference` (registry.py) already has `description`/`purpose`/`injected_as` (v0.7.0/v0.8.0) --
  flat, per-reference, no hierarchy, no cross-references. This epic extends the same additive
  pattern with two more fields.
- `_STRUCTURED_TAG_FIELDS = ("provider", "project", "env", "scope", "kind")` — description/
  purpose/injected_as were deliberately excluded (informational, not tag-matchable identity).
  `group`/`related` follow the same precedent: informational, not resolve_by_tags()-matchable.
- `Registry.add()`/`Registry.retag()` both already have the additive-field extension pattern
  (most recently for description/purpose/injected_as in portunus-vault-metadata-ui story 01) --
  `group`/`related` slot into the same shape.
- CLI's `_parse_tags()` helper (`k=v,k2=v2` -> dict) is reused everywhere a flat dict-shaped
  field needs CLI parsing (injected_as, tags). `related` is list-shaped, not dict-shaped, so it
  needs a sibling helper (`k,k2,k3` -> list) rather than reusing `_parse_tags()` directly.
- No existing command renders anything tree-shaped. `portunus list --project X` prints a flat
  list. Building `portunus tree` means: fetch references (optionally filtered by project), split
  each non-empty `group` value on `/`, build a nested structure, render as ASCII tree (text
  mode) or nested JSON (--json mode, the UI/LLM consumer format, same convention as
  `discover --json`/`list --json`).
- UI: `ProjectExplorer.tsx` (shipped in portunus-vault-metadata-ui) already fetches the full
  flat reference list for a project via `/api/list` -- every `Reference` field, including
  whatever this epic adds, arrives automatically once the TS type is updated (no new route
  needed for the UI's tree view; it derives the tree client-side from data it already has,
  mirroring the CLI's own from-flat-list tree-building logic -- two independent implementations
  of the same small algorithm, consistent with this codebase's existing pattern of a Python
  helper and a parallel TS helper for shared concepts, e.g. `_parse_tags()` / `tagsToArg()`).
- `DetailDrawer.tsx`'s Move form is the established single edit surface for all metadata
  (extended twice already: provider/project/env in portunus-agent-ops-federation,
  description/purpose/injected_as in portunus-vault-metadata-ui) -- `group`/`related` are a
  third extension of the same form/handler, not a new edit surface.

## Constraints (cross-cutting-concerns.yaml)

`secret-boundary-invariant` applies as always -- `group`/`related` are pure metadata (a path
string, a list of reference names), never values, never a new way to reach one.
`audit-chain-integrity` — no new audit entry type needed; these fields flow through the
existing `retag` audit action, same as description/purpose/injected_as.

## Scope decision for this pass

- `related` names are NOT validated against the registry at write time (no ordering constraint
  on which secret you add first -- you can describe a relationship to a secret that doesn't
  exist yet). Read-side (tree/list rendering) marks an unresolved related name as broken rather
  than silently hiding it or hard-failing the write.
- Tree rendering lands in Project Explorer (already project-scoped, already the "explore what's
  here" surface) rather than a new tab or retrofitted into Vault Map's existing
  provider-grouping -- keeps the diff contained and puts the tree where a hierarchy naturally
  belongs.
- Console's table stays untouched (already dense; group/related display lives in DetailDrawer
  and the Project Explorer tree, not a sixth table column).
- The settings-page / backend-mode-configuration question from the same user turn is explicitly
  a separate, later epic -- not addressed here.
