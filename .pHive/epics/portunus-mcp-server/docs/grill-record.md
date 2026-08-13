# Grill Record — portunus-mcp-server

**Source draft:** .pHive/epics/portunus-mcp-server/docs/design-discussion.md
**CONTEXT.md substrate:** present
**inconsistency_risk_signals:** absent (heuristic pass)
**round_number:** 1
**unresolved_count:** 1
**Generated:** 2026-08-14T01:00:00Z

## Summary

- Vocabulary mismatches: clean
- Hidden assumptions: 1 finding
- Unresolved tensions: clean
- Convention violations: clean
- Posture mismatches: clean

## Hidden assumptions

- **H1** — §3 Slices D/E describe both injection tools' addressing parameter as
  `tags_or_name` without ever defining its actual shape. `Resolver.resolve_to_tempfile(text)`
  resolves `{{secret:NAME}}` placeholders embedded in arbitrary text — it needs an exact
  reference *name*, not a tags dict. The draft never says whether the calling agent is
  expected to already know the exact name (two round-trips: list/ask_preview first, then
  inject) or whether the tool itself should accept tags and resolve internally (one round-trip,
  matching how `inject --tags`/`ask --target` already work at the CLI layer).
  - Draft location: §3 Slice D, Slice E
  - Why this matters: this is the exact parameter shape an agent has to get right on every
    call — an undefined param shape here isn't a detail, it's the tool's actual interface.
  - Question for planner: resolving now — both injection tools accept `name: str = ""` OR
    `tags: dict = {}` (at least one required), mirroring the CLI's own dual addressing
    convention. Resolution: `registry.require(name)` if `name` is given; else
    `registry.resolve_by_tags(**tags)` if `tags` is given (fail-closed `NoMatch`/
    `AmbiguousMatch` surfaced as a clear tool error, same as the CLI). Once resolved, the tool
    builds the `{{secret:<resolved.name>}}` placeholder itself and passes that to
    `Resolver.resolve_to_tempfile()`/`resolve_exec()` — the agent never constructs placeholder
    syntax itself, it just says what it wants (a name it already has from a prior `list`/`tree`/
    `ask_preview` call, or tags it already knows).

## Notes

One real, load-bearing gap — the addressing parameter shape for the two tools an agent will
actually call to get something injected. Resolved by reusing the CLI's own established
dual-addressing convention rather than inventing a new one.

## Out of scope (this pass)

Grill does NOT propose solutions, score quality, gate work, or prioritize findings.
