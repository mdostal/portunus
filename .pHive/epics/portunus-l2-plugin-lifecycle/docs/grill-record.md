# Grill Record — portunus-l2-plugin-lifecycle

**Source draft:** .pHive/epics/portunus-l2-plugin-lifecycle/docs/design-discussion.md
**CONTEXT.md substrate:** present
**inconsistency_risk_signals:** absent (heuristic pass)
**round_number:** 1
**unresolved_count:** 1
**Generated:** 2026-08-13T00:00:00Z

## Summary

- Vocabulary mismatches: 1 finding
- Hidden assumptions: clean (draft already flags the standalone-mode file-tracing risk with a
  real verification plan, not just "build succeeds")

## Vocabulary mismatches

- **V1** — CONTEXT.md has no entry for "L2 plugin lifecycle," "standalone output," or the
  capabilities list once this ships.
  - Question for planner: fold into the story's own docs update.

## Notes

One additional hidden-assumption risk surfaced during review (not from the draft's own §4, an
independent check): Next.js's `output: "standalone"` build does NOT automatically include
`public/` or `.next/static/` in the traced output -- Next's own docs require manually copying
both alongside the standalone server for the app to render correctly (missing CSS/assets
otherwise). The design discussion's verification plan ("curl a real API route") would NOT
catch a missing-static-assets bug, since API routes don't serve static files. Resolving now:
the verification step must also fetch the actual HTML page (`/`) from the standalone server
and confirm the CSS bundle loads, not just an API route.

## Out of scope (this pass)
