# Vertical Planning — Slice Plan: portunus-standalone-core

## 1. Slicing Strategy

```
STRATEGY:
  Total horizontal items: ~24
  Planned slices: 7
  First slice goal: registry supports structured tags + fail-closed resolution, with zero
    behavior change for existing callers (backward compatible, unit-tested in isolation)
  Final slice goal: CONTEXT.md updated, portunus verify covers every new audit entry type,
    session-vault epic's disposition resolved

  Slicing rationale: Registry (tags + resolve_by_tags + write lock) has to exist before
    anything can consume it, so it's the mandatory first cut. From there, each slice adds
    exactly one new consumer of resolve_by_tags() (CLI query, then adapters, then semantic
    front door, then UI) so a bug is always traceable to the slice that introduced it. HTTP
    adapters are sequenced after env/file (lower risk first, per design-discussion §6 Q5).
    UI is last because it depends on every prior layer having a stable surface to call.
```

## 2. Vertical Slice Plan

```
## Step 1: Tag schema + migration + write lock

WHAT WORKS AFTER THIS STEP:
  Reference records carry provider/project/env/tags{} alongside the existing scope/kind
  fields (both readable). resolve_by_tags() exists, is unit-tested for the fail-closed
  contract (0 matches -> NoMatch, >1 match -> AmbiguousMatch, exactly 1 -> the Reference),
  and existing scope/kind-based code paths are unaffected. Registry writes are now
  lock-serialized.

LAYERS TOUCHED:
  Registry:
    - Reference gains provider/project/env/tags fields
    - resolve_by_tags(partial_tags) -> Reference
    - migration path for existing references (scope/kind -> tags, additive not destructive)
    - file write lock around registry mutation

NOT YET:
  - No consumer wired up yet (CLI, adapters, semantic ops, UI all untouched)
  - HTTP/env/file adapters
  - CONTEXT.md not yet updated (deferred to Step 7 once schema is stable)

VERIFIED BY:
  - pytest: resolve_by_tags() ambiguity/no-match/exact-match cases
  - pytest: migration produces valid tags{} for every pre-existing scope/kind combination
  - pytest: concurrent-write test (two writers, lock serializes them, no corruption)

COMMIT REPRESENTS: Registry supports structured tags with fail-closed resolution — foundation only

---

## Step 2: CLI tag-query consumer

BUILDS ON: Step 1
WHAT WORKS AFTER THIS STEP:
  A human can run a CLI command to find a reference by tags and see its metadata (never a
  value) — the first real, working consumer of resolve_by_tags().

LAYERS TOUCHED:
  CLI:
    - portunus find --tags provider=vercel,project=mdostal.com -> prints matching
      reference metadata or a clear ambiguous/no-match error

NOT YET:
  - Injection (no adapters yet — this step only proves resolution end-to-end)
  - Semantic/natural-language parsing (exact tags only)

VERIFIED BY:
  - pytest: CLI integration test — exact match, ambiguous match (error), no match (error)
  - Manual: run against a local vault with 2+ tagged references

COMMIT REPRESENTS: portunus find --tags works end-to-end against the real registry

---

## Step 3: Env + File injection adapters

BUILDS ON: Step 1, Step 2
WHAT WORKS AFTER THIS STEP:
  A resolved reference's value can be injected into a process environment variable or written
  into a templated file (.env/JSON/YAML), via a new `portunus inject` CLI subcommand — boundary
  invariant intact (value never printed/returned/logged), and every injection produces an
  adapter_resolution audit entry.

LAYERS TOUCHED:
  Resolver:
    - EnvVarAdapter, FileAdapter
  CLI:
    - portunus inject --tags ... --target env|file [--format env|json|yaml]
  Audit:
    - adapter_resolution entry type

NOT YET:
  - HTTP header/body adapters (Step 5)
  - Semantic/natural-language front door (Step 4)
  - UI

VERIFIED BY:
  - pytest: each adapter asserted to never return/log/print the value (boundary-invariant test)
  - pytest: adapter_resolution audit entries written correctly; portunus verify passes on a
    chain including them
  - Manual: inject into a real env var / real file, confirm value present at destination only

COMMIT REPRESENTS: First two injection adapters work end-to-end, audited, boundary-safe

---

## Step 4: Semantic front door

BUILDS ON: Step 1, Step 2, Step 3
WHAT WORKS AFTER THIS STEP:
  An agent (or human) can express intent in natural language ("inject the vercel secret for
  mdostal.com into env") via `portunus ask` or a thin Claude skill wrapper, which parses to a
  concrete tag set (failing closed with a clarifying question on ambiguity — never guessing)
  and dispatches to Step 3's adapters.

LAYERS TOUCHED:
  Resolver:
    - parse_intent(natural_language) -> tag_set | AmbiguousIntent
  CLI:
    - portunus ask "<request>"
  Agent surface:
    - Claude skill wrapping portunus ask
  Audit:
    - semantic_op entry type

NOT YET:
  - HTTP adapters
  - UI
  - Add/rotate via semantic ops (v1 semantic front door is fetch/inject only; add/rotate stay
    CLI-drop-only per the design discussion's boundary decision)

VERIFIED BY:
  - pytest: parse_intent ambiguity cases return AmbiguousIntent, not a guess
  - pytest: semantic_op audit entries; portunus verify passes
  - Manual: run portunus ask against 2+ plausible-sounding but ambiguous requests, confirm
    it asks for clarification instead of picking one

COMMIT REPRESENTS: End-to-end semantic request -> injection, fail-closed at every step

---

## Step 5: HTTP header + body adapters

BUILDS ON: Step 3
WHAT WORKS AFTER THIS STEP:
  Injection into an outbound HTTP request's header or JSON body field works via the same
  `portunus inject --target http-header|http-body` surface, same boundary/audit guarantees.

LAYERS TOUCHED:
  Resolver:
    - HttpHeaderAdapter, HttpBodyAdapter
  CLI:
    - portunus inject --target http-header|http-body extended

NOT YET:
  - UI

VERIFIED BY:
  - pytest: boundary-invariant tests for both new adapters (no value in logs/returns)
  - pytest: audit entries + portunus verify
  - Manual: inject into a real outbound request (e.g., a test HTTP server) and confirm receipt

COMMIT REPRESENTS: Full adapter set (env/file/http-header/http-body) complete

---

## Step 6: UI v1

BUILDS ON: Step 1, Step 2, Step 3, Step 4
WHAT WORKS AFTER THIS STEP:
  A human can open a localhost-only Next.js UI, see the list of references (tags + state,
  never values) and each one's audit trail, add a new secret (submits to the same
  harness-side-only local drop path, never through an LLM), and trigger a rotation.

LAYERS TOUCHED:
  UI:
    - Reference list, reference detail (audit trail), add-secret form, rotate action
  Broker:
    - confirm UI add/rotate route through the existing gated path, no bypass
  Audit:
    - ui_action entry type

NOT YET:
  - Remote/non-localhost deployment
  - L2 Pantheon plugin lifecycle wiring (explicitly out of this epic)

VERIFIED BY:
  - Manual: full add/view/rotate flow exercised locally
  - pytest (backend side): UI's API calls route through Broker.check_injectable, not around it
  - pytest: ui_action audit entries; portunus verify passes

COMMIT REPRESENTS: Standalone UI v1 — the biggest single north-star capability landed

---

## Step 7: Glossary + verification closeout + session-vault disposition

BUILDS ON: all prior steps
WHAT WORKS AFTER THIS STEP:
  CONTEXT.md reflects the new vocabulary (tag schema, adapter, resolve_by_tags). portunus
  verify is confirmed against a full chain spanning every new entry type introduced by this
  epic. portunus-session-vault's stories 01/02 are folded into the new tag schema (session
  credentials become a `kind` under it); 03-05 are re-evaluated against the broker/UI that
  now exists and either adapted or explicitly closed.

LAYERS TOUCHED:
  Docs:
    - .pHive/CONTEXT.md Terminology section
  Audit:
    - final portunus verify pass across all new entry types together
  Epic bookkeeping:
    - portunus-session-vault stories updated/closed per the fold-in decision

NOT YET:
  - (this is the epic's closing slice)

VERIFIED BY:
  - portunus verify against a chain containing every entry type from Steps 1-6
  - Manual: CONTEXT.md read-through against the actual shipped code

COMMIT REPRESENTS: Epic closeout — glossary current, audit chain fully verified, prior epic
  reconciled instead of silently orphaned
```

## 3. Overlay Diagram

```
VERTICAL SLICE OVERLAY
─────────────────────────────────────────────────────────────────────────────────

              │ Step 1    │ Step 2   │ Step 3    │ Step 4    │ Step 5   │ Step 6  │ Step 7 │
              │ (tags)    │ (query)  │ (env/file)│ (semantic)│ (http)   │ (UI)    │ (close)│
──────────────┼───────────┼──────────┼───────────┼───────────┼──────────┼─────────┼────────┤
Registry      │ schema +  │          │           │           │          │         │        │
              │ lock      │          │           │           │          │         │        │
──────────────┼───────────┼──────────┼───────────┼───────────┼──────────┼─────────┼────────┤
Resolver      │           │          │ Env/File  │ parse_    │ Http     │         │        │
              │           │          │ Adapter   │ intent()  │ Adapter  │         │        │
──────────────┼───────────┼──────────┼───────────┼───────────┼──────────┼─────────┼────────┤
CLI           │           │ find     │ inject    │ ask       │ inject   │         │        │
              │           │ --tags   │ (env/file)│           │ (http)   │         │        │
──────────────┼───────────┼──────────┼───────────┼───────────┼──────────┼─────────┼────────┤
Agent surface │           │          │           │ Claude    │          │         │        │
              │           │          │           │ skill     │          │         │        │
──────────────┼───────────┼──────────┼───────────┼───────────┼──────────┼─────────┼────────┤
Audit         │           │          │ adapter_  │ semantic_ │ (reuse)  │ ui_     │ verify │
              │           │          │ resolution│ op        │          │ action  │ pass   │
──────────────┼───────────┼──────────┼───────────┼───────────┼──────────┼─────────┼────────┤
UI            │           │          │           │           │          │ list/   │        │
              │           │          │           │           │          │ add/    │        │
              │           │          │           │           │          │ rotate  │        │
──────────────┼───────────┼──────────┼───────────┼───────────┼──────────┼─────────┼────────┤
Docs          │           │          │           │           │          │         │CONTEXT │
              │           │          │           │           │          │         │.md     │
─────────────────────────────────────────────────────────────────────────────────

Each column is a commit-worthy, working state.
```

## 4. Deferred Items

```
DEFERRED (not in current slice plan):
  - L2 Pantheon plugin lifecycle wiring (manifest.json descriptor + lifecycle events) —
    explicitly secondary per north_star; separate epic once standalone core is proven
  - Remote/non-localhost UI deployment — v1 is localhost-only by design (Grill U1 resolution)
  - GCP Secret Manager native labels as a tag source — evaluated and rejected in the design
    discussion (Grill H1); local-registry-owned tags stay authoritative
  - MCP server for the agent-facing surface — CLI + Claude skill cover v1; MCP only if a
    concrete need emerges

RATIONALE: All four are either explicitly out of scope per north_star (plugin lifecycle),
  a deliberate v1 constraint with a documented reason (localhost-only, local-registry tags),
  or genuinely premature (MCP server without a proven need).
```

## 5. Risk by Slice

```
RISK PER SLICE:
  Step 1: High — this is the foundation every later step depends on; a bug in
    resolve_by_tags()'s fail-closed contract or the migration propagates everywhere
  Step 2: Low — thin CLI wrapper over Step 1, no new invariants
  Step 3: Medium — first real value-handling adapters; boundary-invariant tests are the
    load-bearing verification here
  Step 4: Medium — parse_intent's own fail-closed behavior is new territory (NLP-adjacent,
    not just exact matching)
  Step 5: Low — same shape as Step 3, applied to two more target types
  Step 6: High — biggest net-new surface (UI stack, human-entry-point boundary decision from
    Grill U1), most likely to reveal UX/scope surprises
  Step 7: Low — bookkeeping and verification, not new capability
```

## 6. Moldability Notes

- Steps 4 and 5 can be reordered (semantic front door vs. HTTP adapters don't depend on each
  other, only both depend on Step 3's adapter abstraction existing) if HTTP adapters turn out
  to be more urgent than the semantic layer.
- Step 5 (HTTP adapters) can be dropped from this epic and pushed to a follow-up if Step 6
  (UI) turns out to need more runway than expected — env/file adapters plus the semantic
  front door already deliver most of the north-star value.
- Step 6 (UI) is the most likely to spawn its own sub-slices once the UI designer weighs in
  during `/design` delegation (Step 13/16 of `/plan`'s UI-story detection) — treat the plan
  above as a starting scope, not a fixed spec.
- Step 7's session-vault reconciliation could surface work that becomes its own small epic
  if stories 03-05 need substantial rework rather than a straightforward fold-in.
