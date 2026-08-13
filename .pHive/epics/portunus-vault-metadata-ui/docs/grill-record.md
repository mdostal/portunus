# Grill Record — portunus-vault-metadata-ui

**Source draft:** .pHive/epics/portunus-vault-metadata-ui/docs/design-discussion.md
**CONTEXT.md substrate:** present
**inconsistency_risk_signals:** absent (heuristic pass)
**round_number:** 1
**unresolved_count:** 1
**Generated:** 2026-08-13T19:00:00Z

## Summary

- Vocabulary mismatches: clean
- Hidden assumptions: 1 finding
- Unresolved tensions: clean
- Convention violations: clean
- Posture mismatches: clean

## Hidden assumptions

- **H1** — §3 Slice D says the Move form gets "description/purpose/injected_as fields" added,
  but `injected_as` is dict-shaped (`{env_name: "env:VAR"|"file:path"}`), not a flat string like
  `description`/`purpose`. The draft doesn't say how a dict-shaped field maps to a form input.
  - Draft location: §3 Slice D
  - Why this matters: without an explicit convention, this could get implemented as an ad-hoc
    JSON-textarea (inconsistent with the rest of the UI) instead of reusing the codebase's own
    existing pattern.
  - Question for planner: resolving now — `injected_as` reuses the exact "k=v,k2=v2"
    comma-separated convention `tags` already uses in `AddSecretForm.tsx`
    (`tags (k=v,k2=v2)` input + the CLI's own `_parse_tags()` helper, which splits only on the
    *first* `=` per pair — so a value like `env:STRIPE_KEY` containing a colon is unaffected).
    Slice A's `--injected-as` CLI flag parses with the same `_parse_tags()` helper (already
    proven, no new parser needed); Slice D's form field gets the same placeholder style:
    `injected_as (env=target,env2=target2)` with an example placeholder like
    `prod=env:STRIPE_KEY,staging=file:.env.staging`.

## Notes

Small draft, one real gap (dict-shaped field UI representation), resolved by reusing an
existing, already-proven convention rather than inventing a new one — no scope change.

## Out of scope (this pass)

Grill does NOT propose solutions, score quality, gate work, or prioritize findings.
