# Design Discussion: portunus-agent-ops-federation

## 1. What Are We Doing?

Three gaps flagged during `portunus-standalone-core`'s traceability check, all in one
Medium-scope pass: (1) let an agent *request* a secret be added or rotated via `portunus ask`
without ever supplying or seeing the value itself, (2) let the UI move/re-tag a reference
between provider/project/env, (3) let a single CLI invocation point at a different repo's
vault via `--home`, so "across all repos" stops meaning "one repo at a time by manually
setting PORTUNUS_HOME."

"Done" means: an agent can say "I need a secret for X" and get a durable, visible, human-
actionable request instead of silence or an error; a human can re-tag a reference without
delete-and-recreate; and any of these CLI commands can target an explicit vault path without
touching the ambient `PORTUNUS_HOME`.

## 2. What I Found

`paths.home()` resolves `PORTUNUS_HOME` once per process — no override point exists. `Registry`
has no in-place tag-update method. `intent.py`'s `parse_intent()` only ever produces a lookup
tag set; there's no "intent kind" concept (fetch vs. request-add vs. request-rotate).
`VALID_STATES` has no "requested" state. The non-negotiable invariant (an agent never sees a
value) means agent-initiated add/roll can only ever be a *request*, never a fulfillment — the
actual value still flows through `drop`, human-originated, exactly as today.

## 3. My Proposed Approach

**Slice A — Agent requests (add/rotate).** Add `requested` to `VALID_STATES` (fail-closed like
`dropped`/`revoked` — never injectable). `Registry.request(name, tags, kind)` creates a
value-less placeholder reference in `state=requested`. Extend `parse_intent`'s contract with an
explicit `intent_kind` output (`fetch` | `add` | `rotate`), inferred from a small set of verb
keywords in the request text (fail closed to `fetch` — the safest default — if ambiguous, per
Grill review below). `portunus ask` routes `add`/`rotate` intents to `Registry.request()`
instead of `resolve_by_tags`, writes a `semantic_op` audit entry (`requested:add`/
`requested:rotate`), and prints/returns a human-actionable notice. The UI's Console/Vault Map
surface `requested` references distinctly (a pill state) so a human sees the ask and fulfills
it via the existing add-secret form.

**Slice B — Move/re-tag.** `Registry.retag(name, **new_tags)` updates provider/project/env/tags
in place (through the same write-lock path as every other mutation), validated the same way
`resolve_by_tags` validates — no silent overwrite into an ambiguous state (reject if the new
tag combination would collide with another existing reference). New `portunus retag <name>
--provider ... --project ... --env ... --tags ...` CLI subcommand and a UI "Move" action
(Console row menu + Vault Map card) that opens a small inline form.

**Slice C — Explicit vault targeting.** A `--home <path>` global CLI flag that overrides
`PORTUNUS_HOME` for that invocation only (`paths.home()` gains an optional override param
threaded through `_build()`). No new registry format, no cross-vault search — just "point this
one command at a different vault." The UI's backend routes already shell out to the CLI per
process env, so a future UI vault-switcher can reuse this without further backend change.

## 4. What Could Go Wrong

- **[high] `intent_kind` misclassifies a fetch as an add/rotate request** — the exact "silent
  wrong action" risk this whole product exists to prevent, just at the request layer instead
  of the value layer. Mitigation: fail closed to `fetch` (the existing, already-safe path) on
  any classification uncertainty; `add`/`rotate` only fire on an unambiguous, narrow keyword
  set ("add", "create", "new secret", "rotate", "roll", "regenerate").
- **[medium] `requested` references accumulate as clutter** with no expiry/cleanup path. Out of
  scope for this pass — flag as a known gap, not a blocker.
- **[medium] `retag` collides two references into the same tag combination silently.**
  Mitigation: `retag` must call the same ambiguity-checking logic `resolve_by_tags` uses before
  committing — reject if the target tag set already resolves to a different existing reference.
- **[low] `--home` typos silently create a fresh empty vault** (since `home()` already
  `mkdir`s). Mitigation: no behavior change from today's `PORTUNUS_HOME` semantics — same risk
  already exists, not introduced by this slice; not worth special-casing.

## 5. Dependencies and Constraints

- Slice A depends on nothing new beyond existing `Registry`/`intent.py`.
- Slice B is independent of A.
- Slice C is independent of A and B, and lowest risk.
- `secret-boundary-invariant` and `audit-chain-integrity` apply throughout — nothing in this
  epic touches a value except through paths already covered by existing boundary tests.
- *(Grill C1)* CONTEXT.md needs `requested` state and `retag` added to Terminology once this
  ships — folded into the closeout story, same pattern as the prior epic's Grill C1.

## 6. Open Questions

1. Should `requested` references be visible in `portunus find`/`reg show` by default, or
   filtered out unless asked for? *(My call: visible — hiding them defeats the "human sees the
   ask" purpose. Grill U1: resolved — default output already labels lifecycle state
   per-reference, so no separate filter needed for a first pass; revisit if usage shows
   clutter.)*
2. Does `retag` need its own audit entry type, or reuse an existing one? *(My call: new
   `retag` action string — it's a distinct, auditable metadata change, not a value-adjacent
   event, so it doesn't need to reuse `adapter_resolution`/`semantic_op`.)*
3. *(Grill H1)* Intent-classification keyword set will start narrow and may miss real add/
   rotate phrasings. Acceptable: false-negative-to-fetch is the safe failure mode (does
   nothing new, rather than misfiring), unlike a false-positive which would need to be rare.
   Ship narrow, widen from real usage later.

## 7. Verification Strategy

```
VERIFICATION PLAN:
  Tools: pytest (existing)
  Automated: intent_kind classification (fail-closed-to-fetch on ambiguity), Registry.request()
    creates a non-injectable placeholder, Registry.retag() rejects tag-collisions, --home
    override routes to the correct vault, UI state-pill rendering for `requested`
  Manual: UI move-action flow, UI requested-state visibility
  Not verifying: cross-vault federated search (explicitly out of scope, see research brief)
```

## 8. Scale Assessment

```
SCALE ASSESSMENT:
  Files affected: ~10 (registry.py, intent.py, cli.py, 3 new/changed UI components, audit
    convention doc update)
  Subsystems: Registry, semantic front door, CLI, UI
  Migration required: no (additive VALID_STATES entry, additive methods)
  Cross-team coordination: no
  Unknowns: 2 (see Open Questions), both low-stakes and already defaulted above

  RECOMMENDATION: Proceed to stories (skip H/V) -- well-understood extension of an
    already-shipped, already-tested foundation; three independent, contained slices.
  RATIONALE: Medium scope by file count, but low architectural risk -- no new subsystems,
    every slice builds on primitives (resolve_by_tags, the write lock, resolve_call) already
    proven in portunus-standalone-core. H/V planning would mostly restate this document.
```
