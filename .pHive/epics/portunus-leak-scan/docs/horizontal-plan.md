# Horizontal Planning Scan: portunus-leak-scan

## 1. Layer Inventory

| Layer | Touched? | Why |
|---|---|---|
| Registry (`registry.py`) | No | Leak status is scan-derived, transient-ish state, not identity/routing metadata about a reference. A new sibling store, not a new `Reference` field (matches `views.py`/`roles.py` precedent, not the `org` field precedent). |
| New: leak-scan engine (`leakscan.py`) | Yes | The core: enumerate configured paths, get decrypted values, search, record findings, never leak a value itself. |
| New: leak-status store (`leakstatus.py` or same module) | Yes | Per-reference escalation state, persisted `PORTUNUS_HOME/leak-status.json`, locked from day one. |
| New: scan-path config store | Yes | Where to scan -- explicit, persisted, empty by default. Could be its own file (`leak-scan-config.json`) or a section of leak-status.json; keep separate so status (frequently rewritten) and config (rarely rewritten) don't share one lock's contention. |
| Backend (`backend.py`) | Read-only use | `Backend.access()` is the existing call the scanner uses to get a value transiently -- no new backend method needed. |
| Broker (`broker.py`) | Decision point, not touched in v1 | Whether leak status ever gates `check_injectable()` is the biggest open policy question this epic raises. Leaning: NOT in v1 (see design-discussion self-grill) -- advisory only, matching `roles.json`'s own "stub, not enforced" precedent, with a genuine test proving it (byte-identical behavior with/without leak findings), same discipline as `test_check_injectable_and_retag_are_byte_identical_with_or_without_roles_configured`. |
| Audit (`audit.py`) | Extend | A leak-scan run and each finding should be audited (`action="leak-scan"` or similar) -- `secret` is the reference name, never the matched value, matching every existing audited action. |
| CLI (`cli.py`) | New subcommands | `portunus leak-scan [--json]`, `portunus leak-scan config add-path/remove-path/show`, `portunus leak status [name]`, `portunus leak mark-rotated <name>`. |
| MCP server (`mcp_server.py`) | New, read-only only | Expose leak STATUS (already-computed findings, severities) to agents -- never a "trigger a scan of arbitrary local paths" tool for v1 (see design-discussion §2 for why). |
| UI (Settings page) | New section | "Leak scan" -- configured paths, last-scan time, findings list (ref/severity/file/line, never a value or excerpt), "Run scan now", "Mark rotated" per finding. |
| Rotation (`rotation.py`) | Read-only use | Escalation messaging can reference whether a real or stub `RotationAdapter` exists for the leaked reference's provider -- reused, not duplicated. |
| Infra/config | New | Default/example scan-path suggestions (e.g. `~/.claude/projects/**/*.jsonl`, shell history) -- suggested, never auto-enabled. |

## 2. Per-Layer Requirements

### Layer: leak-scan engine

- Input: the set of references with a resolvable value (skip `dropped`/`requested`/anything
  `check_injectable` would refuse -- no reason to search for a value that can't legitimately be
  in use) × the configured scan paths.
- For each reference: get its value via `resolver.backend_for(ref)` / `backend.access(...)`,
  transiently, in a scope that never returns or logs it.
- For each configured path (file or glob), read new bytes since that file's last-scanned
  watermark (offset + a fingerprint of `(size, mtime)` -- if the fingerprint indicates the file
  shrank or was replaced, rescan from 0 rather than skipping, since a rotated log is a NEW file
  in spirit even if the path is reused).
- Multi-pattern search: with ~400 possible patterns and multi-GB files, a compiled single-pass
  search (stdlib `re`, alternation of `re.escape()`'d literals, or a hand-rolled trie/Aho-Corasick
  if `re`'s alternation proves too slow in practice -- a real perf check during implementation,
  not assumed) beats a per-pattern `bytes.find()` loop that re-reads the buffer once per secret.
- On a match: record `{ref_name, path, line_number, byte_offset, detected_at}` to the
  leak-status store. NEVER record the matched substring, a context window around it, or any
  form of "here's what we found" excerpt -- name/file/line/offset is enough for a human to go
  look, and looking is the human's own already-consented-to access to their own files.
- Never raise an exception that could embed the value (wrap the search in a scope where the
  value variable never crosses a function boundary that could appear in a traceback beyond the
  search function itself).

### Layer: leak-status store

- `LeakFinding(ref_name, path, line_number, byte_offset, first_detected_at, last_detected_at)`
  -- re-detecting the same (ref, path, line) updates `last_detected_at`, doesn't duplicate.
- `LeakStatus(ref_name, severity, findings: [LeakFinding], rotated_at: Optional[str])` --
  severity is DERIVED (from elapsed time since `first_detected_at` across all findings for that
  ref, and/or from finding count) at read time, not stored redundantly out of sync with its
  inputs -- same "derive, don't duplicate" discipline `completeness.ts` already established.
- `mark_rotated(ref_name)` clears active findings for that ref (or sets `rotated_at`, and a
  NEW post-rotation detection starts a fresh escalation clock -- old findings before rotation
  shouldn't keep escalating a secret that's already been rolled).
- Locked from day one via one `flock_path()` acquisition per mutating operation, matching
  `views.py`/`roles.py`.

### Layer: scan-path config

- A list of path globs, persisted, empty by default. `portunus leak-scan config add-path <glob>`
  / `remove-path` / `show`. Running `portunus leak-scan` with zero configured paths is a no-op
  that says so explicitly (matching `crawl_candidates()`'s own "(no candidates)" honesty), not
  a silent success that looks like it scanned something.
- Ship SUGGESTED defaults as documentation/a `--suggest-defaults` flag or similar (e.g. common
  `.claude` transcript globs, shell history paths) that the user can review and opt into --
  never auto-added to the active config.

### Layer: CLI

- `portunus leak-scan [--json]` -- run a scan now over the configured paths, print/return new
  findings.
- `portunus leak-scan config add-path/remove-path/show`
- `portunus leak status [name]` -- current severity + finding count per reference (or all).
- `portunus leak mark-rotated <name>` -- clear escalation after the human has actually rotated
  the credential out-of-band (Portunus doesn't rotate anything itself here -- v1 has no real
  rotation adapters wired to fire automatically; this is a human's own confirmation).

### Layer: MCP server

- `portunus_leak_status(name="")` -- read-only, returns already-computed severity/finding
  metadata (ref name, severity, finding count, first/last detected) for one or all references.
  Never a file path's content, never a value, never a match excerpt.
- Explicitly NOT exposing a "trigger a scan" or "read file at path" MCP tool in v1 -- an agent
  triggering filesystem reads of the user's own conversation history / logs on its own
  initiative is a materially different trust boundary than an agent reading ALREADY-COMPUTED,
  ALREADY-SCOPED-BY-A-HUMAN status. Scan execution stays a human-triggered CLI/UI action for v1.

### Layer: UI

- Settings "Leak scan" section: configured paths list (add/remove), last-scan timestamp,
  findings table (ref, severity badge, file, line, "mark rotated" button) -- exactly the shape
  every other Settings section this session has used (`views`, `roles`).
- "Run scan now" button -- shells to the CLI, same `runPortunus` pattern as every other route.
- Explicit copy about what's scanned and what isn't, matching the crawl epic's own "this is not
  automatic, here's exactly what it does" honesty requirement.

## 3. Escalation ladder — a first cut, refined in design-discussion

Time-since-first-detected, not just presence/absence, is what "slowly escalates" asks for:

- `warn` (0 -- N days since first detected): a visible badge, nothing more.
- `urgent` (N -- M days): same badge, more prominent styling, surfaced in Console's own
  facet/filter system if that's cheap to wire (reusing the existing facet pattern
  `completeness.ts`'s Metadata facet established).
- `critical` (M+ days, still not rotated): most prominent styling; still advisory-only in v1
  (does NOT block `check_injectable`) -- see design-discussion for why enforcement is
  deliberately deferred, not silently assumed.

Exact day thresholds (N, M) are a product decision to make explicitly in design-discussion, not
to bury as a magic number choice here.
