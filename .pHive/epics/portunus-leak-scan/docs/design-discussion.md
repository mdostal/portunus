# Design Discussion — portunus-leak-scan

## 0. Scanning is line-based, not chunk-based — deliberately

Every configured file is scanned line-by-line, not read whole into memory or chunked with
manual overlap-handling. Two reasons: (a) it gives line numbers for free, which the finding
report needs anyway (`ref_name, path, line_number` -- never a value or excerpt, §1), and (b) it
sidesteps the class of bug where a match spans an artificial chunk boundary and gets missed.
The accepted cost: a secret value that's itself been split across two lines (rare in practice
for the token/key-shaped values this project manages) won't be caught. `.claude` transcripts
are JSONL (one JSON object per line) and shell history/most logs are inherently line-oriented,
so this isn't a contrived fit -- it's the natural shape of every real target path identified in
research-brief.md §3.

The incremental watermark is therefore `(byte offset of the last fully-consumed line, a
(size, mtime) fingerprint of the file at that point)`. A re-scan seeks to that offset and reads
forward. If the current file's `(size, mtime)` is inconsistent with growth-only (size shrank,
or mtime moved backward), treat it as a different file in spirit — rescan from 0. This is the
same "don't trust a reused path to mean reused content" caution log-rotation-aware tools always
need, made explicit rather than assumed.

## 1. The value-never-leaks invariant, extended to a code path that has to CALL `.access()`

Every other module that touches a decrypted value (`resolve`, `inject`, `ask`) either returns
it to a caller who explicitly asked for it (with the invariant being "never LOG it"), or
(`crawl.py`) never calls `.access()` at all. This module is a new third shape: it calls
`.access()` *and* must never let the result escape beyond an in-memory comparison.

Concrete design, not just a stated intention:

- Values are fetched into a local `dict[str, bytes]` (`ref_name -> value`) inside the scan
  function's own stack frame, immediately before the search loop, for exactly the references
  being scanned this run — never cached across runs, never written to the leak-status or
  config stores (which persist findings and paths, never values).
- The search function's signature returns `List[Finding]` where `Finding` has no field capable
  of holding a value or a substring of one (`ref_name, path, line_number, byte_offset` only —
  same shape crawl.py's candidate bundle established for "queryable but never a value").
- Any exception raised inside the per-line search loop is caught and re-raised as a NEW
  exception carrying only the path and a generic message — never `raise` a bare exception whose
  `args` could include the line content or a matched value (a real failure mode this session's
  own `BackendError`/`AmbiguousMatch` messages have always been careful about: message text is
  authored, never `f"...{value}..."`-interpolated from raw input).
- A structural test (matching `crawl.py`'s AST-level "never calls `.access(` " check, inverted:
  this one confirms the module's *return points* and *log/print calls* never reference the
  local values dict or the per-line string being searched, only the `Finding` fields) plus a
  behavioral test that intentionally makes the search raise mid-scan and asserts the resulting
  error message doesn't contain the fixture secret value.

## 2. Why no MCP tool triggers a scan or reads file content

`portunus_crawl_candidates` (portunus-metadata-crawl) is safe to expose read-only to any
MCP-connected agent because it only returns metadata the vault ALREADY has, scoped to
references, nothing outside `PORTUNUS_HOME`. A hypothetical `portunus_run_leak_scan` MCP tool
would be different in kind: it would let an agent cause Portunus to read the CONTENTS of
arbitrary files under a human-configured path (the user's own conversation history, shell
history, logs) at the agent's own initiative, and — even scoped to "only report matches,
never content" — the mere ability to trigger reads of specific paths and observe timing/
existence is a new probing surface an agent didn't have via Portunus before (it may well have
direct filesystem access via its own tools already, but that's a SEPARATE trust boundary, not
one this project should implicitly vouch for by wiring it through Portunus).

`portunus_leak_status(name="")` stays in scope: it returns only what a human already ran and
already chose to compute (severity, finding count, timestamps) — the same "already-known,
already-scoped" posture every other read-only MCP tool in this codebase has. Actually running
a scan stays a human-triggered CLI/UI action, always. This mirrors the crawl epic's own
"the LLM reads a bundle a human's process already assembled, it never gets to reach further on
its own" posture (design-discussion.md §5 of that epic), extended to an even more sensitive
surface here.

## 3. Escalation thresholds — a real decision, not a placeholder

- `warn`: 0–2 days since first detection.
- `urgent`: 3–6 days.
- `critical`: 7+ days.

Rationale: short enough that "warn" genuinely means "just noticed, act soon" rather than
sitting unnoticed for a sprint; `critical` at one week matches the rough order-of-magnitude a
real credential-rotation SLA would target for a known-exposed secret, without being so
aggressive (e.g. same-day critical) that it reads as alarmist noise on day one. These are
implemented as named constants in one place (`leakscan.py` or a small config surface), not
hardcoded inline at each call site, so a later epic revisiting this doesn't have to hunt for
magic numbers.

Multiple findings for the same reference use the EARLIEST `first_detected_at` across all of
them for severity — a secret that leaked into three different log files a week apart is exactly
as urgent as one that leaked once a week ago, not "three separate warn-level findings."

## 4. Enforcement — advisory only in v1, proven the same way roles.json proved it

Research-brief's open question: does leak status ever feed back into `check_injectable()`?
**No, not in v1.** Same reasoning `roles.json` used for RBAC: build the real, persisted,
genuinely-actionable signal first; defer the harder, more consequential decision of WHEN
degraded trust should actually block a resolve (does one `warn`-level finding block? Only
`critical`? Does a human override exist? What breaks for an already-running deploy relying on
a reference that gets blocked mid-flight?) to a future epic once the detection side has proven
itself against real usage.

Proven, not just asserted: `tests/test_leakscan.py` (or wherever this lands) includes a direct
analog of `test_check_injectable_and_retag_are_byte_identical_with_or_without_roles_configured`
— `check_injectable()` and `resolve()` behave byte-identically whether or not a reference has
active leak findings, at any severity. This is the same discipline this session established
specifically because "defaults to permissive" and "is provably inert" are different guarantees,
and only the latter survives a future refactor accidentally wiring something up.

## 5. Minimum value length — skip trivial/short values from search

A resolvable value shorter than a configured minimum (default: 8 characters) is excluded from
the search entirely, not searched-and-likely-false-positive-everywhere. Rationale: a 3-4
character value (a rare but real possibility — a short PIN-like secret, or a placeholder value
during testing) would match constantly in any real text corpus, producing findings that are
noise rather than signal and burning the scan's time budget on searches that can't be
meaningfully acted on. This is a real, named constant (`MIN_SEARCHABLE_VALUE_LENGTH = 8`), not
a silent behavior — `portunus leak-scan` output notes when a reference was skipped for this
reason (name only, never why in a way that implies anything about the value's actual content
beyond "too short to search safely").

## 6. This is a detective control, not a preventive one — said explicitly in the UI/CLI copy

This feature finds secrets that ALREADY leaked; it does nothing to stop the next paste into a
chat window or the next `console.log(process.env.API_KEY)`. The standing project policy (never
act on a credential pasted in chat; flag it and ask the user to rotate — established earlier
this session, unrelated to this epic, but directly adjacent) remains the actual first line of
defense; this epic is a safety net under it, not a replacement. Every surface (CLI help text,
Settings copy, README section) says this plainly, matching the crawl epic's own "this is
context for a human, not automatic" honesty requirement — the parallel failure mode here would
be a user assuming "the scanner will catch it" and treating that as license to paste secrets
more casually.

## 7. `mark-rotated` is a human assertion, not a verified fact

Portunus has no way to independently confirm a credential was actually rotated at its provider
— `Backend.access()` just returns whatever the backend currently holds, and for most backends
(GCP Secret Manager, etc.) a rotation happens entirely on the provider's side; Portunus would
simply start returning the new value on the next resolve, with no signal that distinguishes
"the old leaked value is gone" from "nothing changed." `portunus leak mark-rotated <name>`
clears the finding and resets the escalation clock based on the human's own word, and the CLI/
UI copy says exactly that ("marks this as resolved — Portunus does not verify the credential
was actually rotated at its provider") rather than implying a guarantee that doesn't exist.
A rescan after marking rotated will naturally re-flag the reference if the (still-leaked, old)
value is still present in a file, which is the honest fallback if a human marks something
rotated prematurely.

## Self-grill

- **What if two different references happen to share the same underlying value?** Each is
  searched and reported independently — no cross-reference dedup. A shared credential
  registered under two names IS two separate things needing two separate rotations from
  Portunus's point of view (each reference is used by different consumers, per `repo`/
  `source_files`/`injected_as`), so double-reporting is correct, not a bug to suppress.
- **Could this train users to paste secrets more casually, trusting the scanner to catch it?**
  Addressed directly in §6 — copy is explicit this is a detective, not preventive, control.
- **Binary or non-UTF-8 content in a scanned path?** Decode with `errors="replace"` per line
  rather than crashing the whole scan on one malformed file — matches, if any, would appear in
  a line's decoded (possibly mangled) text; a fully binary file simply produces no matches
  rather than an error. Framed as "best-effort on the paths you configure," not a hard
  guarantee against every possible file format — the honest v1 scope.
- **Should `PORTUNUS_HOME` itself ever be scanned?** Not specially excluded, but also not
  specially included — `registry.json`/`vault-bindings.json`/etc. hold only metadata (never a
  value), and the encrypted local-vault blob won't literal-match a plaintext value by
  construction. No special-case needed; documented so a future reader doesn't wonder why it's
  missing from the horizontal plan.
- **Test fixtures must never resemble a real credential shape from the user's actual vault** —
  standard practice already followed throughout this session (synthetic `sm-x`/`sm-gh-token`-
  style fixture data), restated here because this epic's own tests literally plant "leaked"
  values in fixture files, making the discipline more load-bearing than usual.
- **Does the scan-path config risk becoming a second place secrets could leak (the config
  itself listing sensitive paths)?** Paths, not values — `leak-scan-config.json` holds globs
  like `~/.claude/projects/**/*.jsonl`, not secret material. No different in kind from
  `vault-bindings.json` already recording project names/accounts.
- **Three separate locked stores (config, status, watermarks) — is that over-engineering
  versus one file?** Deliberate: watermarks are rewritten on every scan (high churn, small
  payload), status is rewritten only on new findings/rotations (medium churn), config is
  rewritten only when a human explicitly changes scan paths (rare). Sharing one `flock_path()`
  across all three would serialize a frequent, cheap watermark update behind a lock a
  config-edit is also trying to take, for no benefit — matching this session's own established
  "avoid unnecessary shared contention" reasoning (distinct from `vault-bindings.json`'s
  still-open single-lock-does-everything gap, not repeating it here).

## Scale assessment

Medium-large: one new core module with real algorithmic care required (Slice 1), a new
persisted-state layer with genuine concurrency requirements (Slice 2), a full CLI surface
(Slice 3), a deliberately-scoped read-only MCP tool (Slice 4), and a UI section (Slice 5) —
comparable in shape to portunus-vault-backup's scope, smaller than portunus-vault-trust-and-
access's three-chain epic. `version_bump: minor` — a genuinely new, additive feature, no
breaking change to any existing surface.
