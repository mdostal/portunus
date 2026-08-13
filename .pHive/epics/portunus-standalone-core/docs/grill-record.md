# Grill Record — portunus-standalone-core

**Source draft:** .pHive/epics/portunus-standalone-core/docs/design-discussion.md
**CONTEXT.md substrate:** present
**inconsistency_risk_signals:** absent (heuristic pass)
**round_number:** 1
**unresolved_count:** 6
**Generated:** 2026-08-12T00:00:00Z

## Summary

- Vocabulary mismatches: clean
- Hidden assumptions: 2 findings
- Unresolved tensions: 2 findings
- Convention violations: 2 findings
- Posture mismatches: clean (see U1 cross-reference)

## Vocabulary mismatches

Draft terminology (OSTIARIUS/ARCA/Petitio, lifecycle state, boundary sink, placeholder) is
used consistently with `.pHive/CONTEXT.md`. Clean.

## Hidden assumptions

- **H1** — §2 asserts GCP Secret Manager "doesn't read... resource labels" and §3/§5
  implicitly treats Portunus's own registry as the only sensible source of truth for tags,
  without evidence that GCP Secret Manager's native label support was evaluated and rejected.
  - Draft location: §2, "GcloudBackend.access() ... doesn't query SM's own resource labels"
  - Why this matters: GCP Secret Manager natively supports resource labels. If SM labels could
    serve as (part of) the tag source, Slice 1 might be simpler (sync from SM) or riskier
    (two sources of truth to reconcile) depending on the answer — but the draft doesn't show
    the tradeoff was considered, just asserts local-registry-only.
  - Question for planner: was SM's native label support deliberately rejected as a tag source
    (and why), or is this an unexamined default? Worth one line in §5 either way.

- **H2** — §3 Slice 4 says the UI "reads the registry + audit chain (read-only, never fetches
  values)" and performs writes "by calling into the same broker-gated path agents use," but
  nowhere does the draft address what happens when a CLI drop, a UI operation, and an agent's
  semantic request race on the same local registry file concurrently.
  - Draft location: §3, Slice 4 paragraph
  - Why this matters: `LocalEncryptedBackend` and the registry are file-backed (per
    CONTEXT.md's Key paths). Three concurrent writers (CLI, UI, agent surface) is a new
    condition this epic introduces — today it's CLI-only. A lost update or corrupted registry
    write is a real failure mode, not a hypothetical.
  - Question for planner: does Slice 1 or Slice 4 own adding file locking / a write-serialization
    story, or is single-writer-at-a-time an explicit accepted constraint for v1?

## Unresolved tensions

- **U1** — §3 Slice 4 promises the UI "never fetches values," but a human has to be able to
  paste a plaintext value into *something* to add or rotate a secret via the UI (the draft's
  own Slice 3 goal is "agents ask semantically... without ever seeing the value" — implying a
  human still must be the one who originally provides it). The draft never states where that
  human-entry point lives or whether the UI is that entry point.
  - Draft location: §1 ("a human can open a UI and see every reference, add one, roll one"),
    §3 Slice 4
  - Tension: "UI never fetches/handles values" (implied boundary-only posture) vs. "UI lets a
    human add one" (which requires the value to pass through *something* the human is looking
    at, i.e. very plausibly the UI itself).
  - Question for planner: does the UI's "add" form submit the value to the same harness-side-only
    `drop` path (i.e., the browser is the one boundary-adjacent surface allowed to see a value,
    analogous to a human running `portunus drop --stdin` today), or does "add" always require
    dropping to a file/stdin outside the UI? This needs an explicit answer before Slice 4 is
    scoped, not an implicit one.

- **U2** — §3 Slice 1 requires `resolve_by_tags()` to "fail-closed on any ambiguity, no fuzzy
  best-guess fallback, ever," while §3 Slice 3 wants agents to ask by loose natural-language
  intent ("inject the vercel secret for mdostal.com"). The draft doesn't reconcile how a fuzzy
  natural-language request becomes the strict tag set Slice 1 requires.
  - Draft location: §3 Slice 1 ("fail-closed... no fuzzy best-guess fallback, ever") vs.
    §3 Slice 3 ("ask by intent")
  - Tension: strict fail-closed exact-tag resolution vs. semantic/fuzzy request parsing that,
    almost by definition, has some interpretive slack.
  - Question for planner: is there an explicit parsing/normalization step between "semantic
    request" and "tag set passed to `resolve_by_tags()`," and does *that* step get to guess,
    or does it also fail closed (return "ambiguous, please specify provider/project/env")? If
    the parser can guess, the "no fuzzy fallback, ever" claim in Slice 1 is only true of the
    tag-matching function, not of the end-to-end agent experience — worth being explicit.

## Convention violations

- **C1** — Introducing new domain vocabulary (`tag schema`, `injection adapter`,
  `resolve_by_tags`) without a corresponding CONTEXT.md update plan violates CONTEXT.md's own
  stated update trigger ("A new domain term enters the codebase... Update CONTEXT.md").
  - Draft location: whole document — new terms introduced throughout §2-§3
  - Convention: `.pHive/CONTEXT.md` → "Update triggers"
  - Question for planner: add a story (likely late in Slice 1, or a cross-cutting task) to
    update CONTEXT.md's Terminology section once the tag schema and adapter abstraction land,
    so the glossary doesn't drift from the code on day one of this epic.

- **C2** — §7 Verification Strategy doesn't mention testing `portunus verify` against audit
  entries produced by the *new* paths (adapters in Slice 2, semantic ops in Slice 3), which
  `cross-cutting-concerns.yaml`'s `audit-chain-integrity` concern explicitly requires
  ("`portunus verify` must still pass on a chain including the new entry type").
  - Draft location: §7 VERIFICATION PLAN block — lists boundary-invariant and audit-chain
    tests generally but not `portunus verify` specifically against new entry types
  - Convention: `.pHive/cross-cutting-concerns.yaml` → `audit-chain-integrity` →
    `implementation_checklist`
  - Question for planner: either add "`portunus verify` passes against a chain including
    adapter/semantic-op entries" to §7's Automated line, or confirm it's implied by "audit-chain
    entries for new paths" and make that explicit.

## Posture mismatches

No standalone posture-check document exists for Portunus yet (no `hive/references/`
posture doc in this repo — expected, this is the project's first epic). No findings beyond
what's already captured under U1 (boundary-only posture tension). Not applicable otherwise.

## Notes

The draft is unusually well-grounded in specific file/line citations (a strength, not a
finding) — nearly every claim in §2 traces to a real path. The six open findings above are
mostly about *sequencing and scoping decisions* (H1, U2, C1, C2 are cheap to resolve with one
sentence each) rather than fundamental flaws in the approach; U1 is the one finding worth real
discussion time since it affects how big Slice 4 actually is.

## Out of scope (this pass)

Grill does NOT propose solutions, score quality, gate work, or prioritize findings. Each
finding ends with a question for the planner; the planner's job is to revise the draft (or
document accepted deviations) before stories are written.
