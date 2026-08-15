# Vertical Planning — Slice Plan: portunus-leak-scan

## 1. Slicing Strategy

```
STRATEGY:
  Total horizontal items: ~11 (engine, status store, config store, CLI x4, MCP tool, audit
    integration, UI section, docs/closeout)
  Planned slices: 6
  First slice goal: the scan engine itself, proven never to leak a value, proven to handle real
    scale (incremental watermarks) -- everything else is surface over this core.
  Final slice goal: docs + version bump + one real end-to-end live proof against a fixture
    vault and fixture "leaked" files.

  Slicing rationale, one chain, strictly sequential (unlike portunus-vault-trust-and-access's
  three parallel chains -- this epic doesn't have independent threads, everything genuinely
  builds on the engine):
    engine (Slice 1) -> status/escalation store (Slice 2) -> config store + full CLI (Slice 3)
    -> MCP read-only status tool + audit wiring (Slice 4) -> Settings UI section (Slice 5) ->
    closeout (Slice 6).

  Checkpoint note: Slices 1-3 are the whole functional core, usable end to end from the CLI
  alone (configure paths, run a scan, see status, mark rotated) -- the strongest checkpoint
  candidate if this epic runs long. Slices 4-5 are surface (MCP + UI) over an already-complete
  engine.

  Explicitly NOT in this epic (see design-discussion.md self-grill for the reasoning on each):
    - check_injectable() enforcement based on leak status (advisory only, v1).
    - An MCP tool that triggers a scan or reads arbitrary file content (status query only).
    - Automatic rotation (no real rotation adapters fire from this epic; "mark rotated" is a
      human's own confirmation after rotating out-of-band).
    - Scanning anything by default without the user explicitly configuring paths first.
    - Entropy/pattern-based generic secret detection (e.g. "this looks like an AWS key") --
      this epic only searches for Portunus's OWN known, managed values. Detecting UNMANAGED
      secrets in logs (things never registered with Portunus at all) is a materially different,
      much fuzzier problem, explicitly out of scope.
```

## 2. Vertical Slice Plan

### Slice 1: Leak-scan engine core

**BUILDS ON:** nothing
**WHAT WORKS AFTER THIS SLICE:** `src/portunus/leakscan.py` can, given a registry + a list of
scan paths, get each resolvable reference's value transiently via the existing
`Backend.access()` path, search configured files for literal occurrences, and return findings
as `{ref_name, path, line_number, byte_offset}` -- structurally proven (a test analogous to
crawl.py's AST value-leak check, adapted: proving the search function never returns, logs, or
re-raises with the value present) to never surface the matched value itself. Incremental:
a second scan of an unchanged-except-appended file only reads the new bytes; a shrunk/replaced
file rescans from 0.
**LAYERS TOUCHED:** New module only (`leakscan.py`).
**NOT YET:** no persisted status store, no CLI, no escalation -- this slice returns findings
as plain data structures a test can assert on directly, nothing more.
**VERIFIED BY:** pytest -- literal-match detection, no-match correctness, incremental
watermark behavior (second scan doesn't re-report unchanged content, does report newly
appended content), shrunk-file-rescans-from-0 behavior, the value-never-leaks structural check,
and a real perf sanity check against a multi-MB fixture file (not a full 3.4 GB repro, but
large enough to catch an accidentally-quadratic implementation).
**SHIP CHECKPOINT CANDIDATE:** no (no CLI surface yet to actually use this).

---

### Slice 2: Leak-status store + escalation severity

**BUILDS ON:** Slice 1 (consumes its findings)
**WHAT WORKS AFTER THIS SLICE:** `PORTUNUS_HOME/leak-status.json` persists findings per
reference (deduped by ref+path+line, `last_detected_at` updated on re-detection), locked from
day one via `flock_path()` (matching `views.py`/`roles.py`). Severity (`warn`/`urgent`/
`critical`) is DERIVED at read time from elapsed time since `first_detected_at`, not stored
redundantly. `mark_rotated(ref_name)` clears active findings and resets the escalation clock
for that reference.
**LAYERS TOUCHED:** New module (or extension of `leakscan.py`).
**VERIFIED BY:** pytest -- dedup-on-re-detection, severity derivation at each threshold
boundary, mark_rotated clearing findings and resetting the clock, concurrent-write safety
(matching the real multi-process lock test precedent `views.py`/`localvault.py` both have).
**SHIP CHECKPOINT CANDIDATE:** no.

---

### Slice 3: Scan-path config store + full CLI

**BUILDS ON:** Slices 1-2
**WHAT WORKS AFTER THIS SLICE:** `portunus leak-scan config add-path/remove-path/show`
(persisted, empty by default -- `portunus leak-scan` with nothing configured says so
explicitly, doesn't silently no-op). `portunus leak-scan [--json]` runs a real scan and prints/
returns new findings. `portunus leak status [name]` shows current severity + finding counts.
`portunus leak mark-rotated <name>` clears escalation. Audited: each scan run and each new
finding is appended to the audit chain with the reference NAME only, matching every existing
audited action.
**LAYERS TOUCHED:** CLI, audit integration.
**VERIFIED BY:** pytest -- each subcommand, `--json` output shape, audit entries never contain
a value, `build_parser()` sanity check. This is the first slice usable end to end from a
terminal.
**SHIP CHECKPOINT CANDIDATE:** yes -- functionally complete for a CLI-only user.

---

### Slice 4: MCP read-only status tool

**BUILDS ON:** Slice 3
**WHAT WORKS AFTER THIS SLICE:** `portunus_leak_status(name="")` MCP tool returns
already-computed severity/finding-count/first-detected/last-detected for one or all references
-- read-only, never a file path's content, never a value, never a match excerpt. No
scan-triggering MCP tool is added (deliberate, see design-discussion §2).
**LAYERS TOUCHED:** `mcp_server.py`.
**VERIFIED BY:** pytest, including the same AST-level value-leak structural check every other
MCP tool this session has used.
**SHIP CHECKPOINT CANDIDATE:** yes, with Slice 3.

---

### Slice 5: Settings UI "Leak scan" section

**BUILDS ON:** Slices 3-4
**WHAT WORKS AFTER THIS SLICE:** Settings gains a "Leak scan" section: configured paths
(add/remove), last-scan timestamp, a findings table (reference, severity badge, file, line,
"Mark rotated" button), and a "Run scan now" button. `/api/leak-scan` and `/api/leak-status`
routes shell out to the CLI, matching every existing route's `runPortunus` pattern. Copy is
explicit about exactly what gets scanned (the configured paths, nothing else) and what doesn't
happen automatically (no auto-rotation, no auto-blocking).
**LAYERS TOUCHED:** UI (`SettingsPage.tsx`, two new API routes).
**VERIFIED BY:** `npm run build`; live Playwright against a fixture vault + fixture "leaked"
file, cross-checked against the CLI's own output, same pattern portunus-metadata-crawl's own
Slice 3 used.
**SHIP CHECKPOINT CANDIDATE:** yes.

---

### Slice 6: Closeout

**BUILDS ON:** Slice 5
**WHAT WORKS AFTER THIS SLICE:** README.md, docs/architecture.md (new section), and
`.pHive/CONTEXT.md` document the feature accurately -- advisory-only, configured-paths-only,
never auto-enforcing. Version bump (epic-level `version_bump` decided in design-discussion),
CHANGELOG entry, full pytest suite + `npm run build` clean, one real end-to-end live proof:
a fixture vault with a real secret, a fixture file containing that secret's literal value,
`portunus leak-scan` finds it, severity escalates correctly across a simulated time boundary,
`mark-rotated` clears it -- all cross-checked via CLI and UI.
**LAYERS TOUCHED:** Docs, version sources, CHANGELOG.
**VERIFIED BY:** full suite, build, live proof as described above.
**SHIP CHECKPOINT CANDIDATE:** yes (final).
