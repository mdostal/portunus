# Grill Record — portunus-secret-tree

**Source draft:** .pHive/epics/portunus-secret-tree/docs/design-discussion.md
**CONTEXT.md substrate:** present
**inconsistency_risk_signals:** absent (heuristic pass)
**round_number:** 1
**unresolved_count:** 1
**Generated:** 2026-08-13T22:15:00Z

## Summary

- Vocabulary mismatches: clean
- Hidden assumptions: 1 finding
- Unresolved tensions: clean
- Convention violations: clean
- Posture mismatches: clean

## Hidden assumptions

- **H1** — §3 Slices C/F never say what happens to a reference with NO `group` set. This isn't
  hypothetical: the real vault now has 382 references (registered live this session via
  `portunus-gcp-multi-account`'s discovery flow) and *none* of them have a `group` — discovery
  never sets one. If `portunus tree`/the Project Explorer tree silently drop ungrouped
  references, the feature would render as empty against the exact real data that exists right
  now.
  - Draft location: §3 Slice C (`portunus tree`), §3 Slice F (Project Explorer tree)
  - Why this matters: silently dropping data a tool is supposed to display is a correctness bug,
    not a cosmetic gap -- worse, it would look like the feature is broken on first real use.
  - Question for planner: resolving now — both the CLI tree and the UI tree render an
    `(ungrouped)` bucket at the root containing every reference whose `group` is empty, listed
    flat (no further nesting) alongside whatever grouped subtrees exist. This also resolves the
    UI's "falls back to flat rendering when none have a group" design point cleanly: that's just
    the degenerate case where the whole tree is one `(ungrouped)` bucket and nothing else —
    same code path, not a separate special case.

## Notes

Small, concrete finding grounded in the real data that already exists in the vault as of this
session (not hypothetical) — exactly the kind of gap grill exists to catch before it ships.

## Out of scope (this pass)

Grill does NOT propose solutions, score quality, gate work, or prioritize findings.
