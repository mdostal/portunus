# Design Discussion — portunus-leak-visibility

## 1. A new, independent `LeakBadge` — not a write into `tags.rotation_requested`

Reusing `RotationBadge`'s existing boolean tag would need leak-scan to call `retag()` on every
new finding, which is a real, new write path this feature has never had — `design-discussion.md`
(portunus-leak-scan) went out of its way to prove `check_injectable()`/`resolve()` behavior is
byte-identical regardless of leak status, but never designed for leak-scan itself writing to the
registry. Doing that now, quickly, as a side effect of a UI request, would be exactly the kind
of undocumented boundary-widening this project has consistently avoided elsewhere.

It would also lose information: `rotation_requested` is a single boolean with no "why" — an
agent-requested rotation and a leak-detected rotation are different situations a human should be
able to tell apart at a glance, and only one of them has "leaked in N locations, here's where"
to show.

**Decision: a new `LeakBadge.tsx`, independently driven by leak-status data (fetched by ref
name), visually adjacent to `RotationBadge` but not sharing its state.** Both can appear on the
same reference (an agent asked for a rotation AND a leak was independently found — genuinely
two different facts). Unifying them into one signal is a real, larger design question
(should a leak finding auto-set `rotation_requested`? should confirming one clear the other?)
explicitly deferred, not decided by default here.

## 2. Finding-detail exposure — extend `summarize()`'s callers, not the invariant

The `path`/`line_number` fields already exist in `leak-status.json` and are already considered
safe to expose (Story 01's core design: `Finding`/`LeakFinding` structurally cannot hold a
value). The only change is exposing the LIST, not just the aggregate count, when a caller asks
for ONE specific reference by name — `portunus leak status <name> --json --detail` (new flag,
default off so existing scripts/automation parsing the aggregate shape don't break), the
`/api/leak-status` route (accepts `?name=`), and `portunus_leak_status(name=...)` MCP tool
(adds an optional `findings` array to its existing return shape). Same value-never-leaks
structural check every prior story used — trivially satisfied here since paths/line numbers
were already the whole point of a `Finding`.

## 3. "Leaked in N conversations" — count distinct FILES, not raw finding count

A `.claude` transcript can contain the same secret on multiple lines (confirmed live: the real
scan found `demo-project-483920-google_generative_ai_api_key` at what the raw finding count
made look like far more "locations" than actual distinct conversations, because the same file
can match many times). The badge's headline number should be **distinct file paths**
(`len(set(f.path for f in findings))`), described as "conversations" or "files" depending on
context, with the raw per-line detail available in an expandable list — matching the user's own
literal phrasing ("leaked in 3 conversations") rather than a raw, inflated finding count.

## 4. Report view — a small custom renderer, not a new dependency

`generate_report()`'s output is a narrow, predictable Markdown subset (`#`/`##`/`###` headers,
`-` bullets, `**bold**`, inline `` `code` ``) — the exact same content already rendered fine as
plain text via download. A ~40-line custom line-based converter to React elements covers 100%
of what this function ever produces, preserving `ui/package.json`'s existing 3-dependency
minimalism rather than pulling in a markdown-parsing library for a fully-controlled, narrow
input format.

## 5. Where the badge renders — everywhere a reference's name already renders

Console's reference rows, `DetailDrawer` (with the full expandable finding history — the
richest surface, matching "show the history"), Vault Map's reference cards, Project Explorer's
reference rows. One component, fetched-once-per-page-load leak-status map passed down, not a
new fetch per row — matching `CompletenessBadge`'s own "derive from data already fetched"
discipline exactly.

## Self-grill

- **Does showing per-line locations in a tooltip risk exposing too much?** No — paths and line
  numbers, never content. A user clicking through to a listed conversation file to actually READ
  it is their own already-consented-to access to their own files, unchanged from any other
  Portunus surface.
- **Should the Console/Vault Map facet system (already used for provider/state/metadata
  filtering) get a "leaked" facet too?** Real, useful, small addition — included in scope,
  matching the existing facet pattern exactly (no new mechanism, just a new derived bucket).
- **Should `mark-rotated` be reachable from the badge's tooltip, not just Settings?** Yes —
  the tooltip is the natural point of action once a human sees "why," avoiding a trip back to
  Settings to act on what they just saw.

## Scale assessment

Small-medium: one new backend exposure (findings-detail on an existing endpoint, not a new
concept), one new UI component reused in 4 places, one new report-view route/panel, a small
facet addition. No new stores, no new invariant. `version_bump: minor` (new user-facing
capability, no breaking change).
