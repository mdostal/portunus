# Grill Record — portunus-local-create

**Source draft:** .pHive/epics/portunus-local-create/docs/design-discussion.md
**CONTEXT.md substrate:** present
**inconsistency_risk_signals:** research-brief §"The dominant design question", §"inconsistency_risk_signals"
**round_number:** 1
**unresolved_count:** 3
**Generated:** 2026-08-14T00:15:00Z

## Summary

- Vocabulary mismatches: clean
- Hidden assumptions: 3 findings
- Unresolved tensions: clean
- Convention violations: clean
- Posture mismatches: clean

## Hidden assumptions

- **H1** — §2 Slice A lists `tags`, `injected_as`, `related` as parameters but never states
  their *shape*. The CLI parses these from comma-separated strings (`_parse_tags`,
  `_parse_related`) because argv flags are strings. An MCP tool call carries structured JSON
  arguments, not shell flags — silently reusing the CLI's string-parsing convention here would
  be copying an implementation detail that doesn't apply to this transport.
  - Draft location: §2 Slice A
  - Why this matters: this is a real interface decision an agent has to get right on every call.
  - Resolution: `tags: Optional[dict] = None`, `injected_as: Optional[dict] = None`,
    `related: Optional[list] = None` — native JSON types, no string parsing, no reuse of
    `_parse_tags`/`_parse_related`. Passed straight to `registry.add(...)`.

- **H2** — §6's risk mitigation for the critical risk ("AST-level source check ... no return
  path references a variable holding the raw `value` argument except the `backend.store()` call
  and the immediate `del`") is too loose to be checkable as written. `value` necessarily appears
  in the function body (as the parameter itself, in `backend.store(ref.sm_name, value)`, and in
  `del value`) — an AST check for "value never appears in source" would be a false requirement.
  - Draft location: §6 risk table
  - Why this matters: the story that implements this needs a precise, checkable assertion, not
    an approximate one — this is the epic's one critical-severity risk.
  - Resolution: the precise, checkable claim is narrower — every `return` statement's expression
    (and every exception handler's constructed error dict) must be built ONLY from `ref.name`,
    `ref.sm_name`, `ref.state`, or a fixed string literal — never from the `value` parameter or
    any variable assigned from it. State this exact claim in the story's acceptance criteria so
    the AST test asserts something concrete: walk every `Return` node in the function body,
    confirm none references the name `value`.

- **H3** — §4 makes the philosophical case that a value flowing *in* to `portunus_drop` is not a
  boundary-invariant violation, but that reasoning never gets translated into a concrete
  docstring instruction for the calling agent — unlike `portunus_resolve_exec`, whose docstring
  explicitly tells the caller "Portunus cannot control what the command you supply chooses to
  print." `portunus_drop` has a symmetric gap: after a successful store, nothing stops the
  calling agent from gratuitously re-printing the value it just stored in its own response to
  the human ("I stored your key: xyz") — Portunus's guarantee ends at "the tool's own return
  never contains it," not at "the calling agent will behave."
  - Draft location: §4
  - Why this matters: without an explicit instruction, a future reader could mistake §4's
    philosophy for a completed mitigation rather than a caller-responsibility note that still
    needs to reach the docstring, the same way resolve_exec's carve-out does.
  - Resolution: `portunus_drop`'s docstring must explicitly state that the caller is responsible
    for not echoing the value back to the human/its own output after a successful store — same
    pattern, same place in the docstring, as `resolve_exec`'s caller-echo carve-out.

## Notes

All three findings are real interface/precision gaps, not open product questions — each has a
concrete resolution folded directly into the revised design-discussion draft and into the
stories' acceptance criteria. No finding blocks proceeding to stories.

## Out of scope (this pass)

Grill does NOT propose solutions, score quality, gate work, or prioritize findings.
