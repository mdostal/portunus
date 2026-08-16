# Research Brief — portunus-leak-visibility

## 1. The ask

Immediately after dogfooding leak-scan against the real vault (83 real findings surfaced,
including a Google Generative AI key leaked into 48 locations): "let's get a UI to put this in
and a way to see the report... flagging the keys with the info of why we're flagging... hover
icon that says -- leaked in 3 conversations etc and show the history... link this across."

Three concrete asks: (a) a real report VIEW in the UI, not just a downloadable file, (b) a
visible flag on a reference wherever it renders, with a hover explaining WHY (finding count,
locations), (c) that flag showing up consistently across every surface a reference appears
("link this across"), not just buried in Settings' own Leak scan section (which already
exists, shipped in portunus-leak-scan Story 05, but is the ONLY place leak status is visible
today).

## 2. What already exists — verified, not assumed

- **`leak-status.json` already has everything needed for the hover content.** Each
  `LeakFinding` carries `path`, `line_number`, `first_detected_at`, `last_detected_at`. The
  only thing NOT currently exposed anywhere (CLI, MCP, or UI) is the raw per-finding list for a
  SPECIFIC reference — `summarize()` (leakscan.py) and every consumer of it
  (`portunus leak status`, `portunus_leak_status`, `/api/leak-status`) return only the
  aggregate (`severity`, `finding_count`, `first/last_detected_at`), never the location list
  itself. This is the one real gap, not a redesign.
- **`RotationBadge.tsx` is the closest existing precedent** — a small badge rendered next to a
  reference's name, driven by `reference.tags.rotation_requested === "true"` (set by the
  `portunus ask "rotate ..."` flow, portunus-agent-ops-federation epic). It already answers
  "how does this project surface an actionable flag on a reference" — but it's a single boolean
  with no "why," and it's driven by the registry's own `tags` field, not by leak-status data.
- **`CompletenessBadge.tsx`** is the closest precedent for "a badge derived from data the
  component already has, not a new fetch" — confirms the project's established pattern of
  deriving-not-duplicating wherever possible.
- **`generate_report()` output is plain, predictable Markdown** (`#`/`##`/`###` headers, `-`
  bullets, `**bold**`, inline `` `code` ``) — narrow enough to render with a small custom
  converter, not a reason to add a markdown-parsing dependency. `ui/package.json` has exactly 3
  runtime dependencies today (`next`, `react`, `react-dom`) — the project's own minimal-
  dependency discipline, worth preserving rather than reaching for a library.
- **No existing UI route renders the report** — Settings' "Download report" button only
  triggers a file download (`portunus-metadata-crawl` Story 03); there is no in-app view of it.

## 3. The one real design decision: does leak detection write to the registry?

`RotationBadge` is driven by a registry `tags` field, which only ever changes via `retag()`
(the one path that writes tags, established since portunus-vault-trust-and-access). Making a
leak-detected reference show the SAME badge would mean either (a) leak-scan starts calling
`retag()` on new findings — a new write path this feature has never had, crossing a boundary
`design-discussion.md`'s advisory-only section didn't anticipate or design for — or (b) a
second, independent signal source (a new `LeakBadge`, querying leak-status data directly,
visually similar but not sharing state with `RotationBadge`).

This research brief doesn't resolve that — see design-discussion.md §1 for the actual decision
and reasoning.
