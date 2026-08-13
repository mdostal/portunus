# Design Discussion: portunus-session-cli

## 1. What Are We Doing?

Exposing the session-storage library API (`LocalEncryptedBackend.store_session/load_session/
inspect_session/list_sessions/remove_session`, extended with TTL enforcement in
`portunus-session-ttl-and-list`) via the CLI. Today it's Python-only — no way to store or
inspect a session without writing a script. Adds `portunus session store|load|inspect|list|
remove`, following the exact boundary discipline already established for secrets: a session
payload is exactly as sensitive as a secret value, so it gets the same stdin-only-in, temp-
file-only-out treatment as `drop`/`resolve`.

## 2. What I Found

`store_session()`/`load_session()`/`inspect_session()`/`list_sessions()`/`remove_session()` all
exist and are tested (`portunus-session-ttl-and-list`, just shipped as v0.4.0). None of them are
wired into `cli.py`. None of them touch the audit chain either — unlike `drop`/`resolve`, which
explicitly call `broker.audit.append()` after using the resolver, the session methods are called
directly against the backend with no audit trail at all today. `drop`'s existing pattern
(`if not hasattr(backend, "store"): return _err(...)`) is the right model for gating on the
local-encrypted backend specifically, since sessions are a `LocalEncryptedBackend`-only
capability (not part of the generic `SecretBackend` protocol `GcloudBackend`/`MockBackend`
implement).

`resolve`'s temp-file discipline (`resolver.resolve_to_tempfile()`, 0600, path-only printed) is
the right model for `session load`, since a loaded session record contains the actual
cookies/tokens — printing it to stdout would defeat the entire boundary invariant this project
exists to enforce, exactly the same risk a secret value has.

## 3. My Proposed Approach

Single vertical slice (small enough not to need separate slicing): a new `portunus session`
subcommand group with five actions, each following an existing established pattern:

- **`session store <site> <account> --stdin|--value-file <path> --ttl-seconds N
  [--rotation-interval-seconds N]`** — mirrors `drop`: value (the whole session JSON) via stdin
  or file only, never inline argv. Requires the local-encrypted backend.
- **`session inspect <site> <account> [--json]`** — metadata only, safe to print directly
  (mirrors `find`).
- **`session list [--json]`** — metadata for every stored session (mirrors `audit --json`).
- **`session load <site> <account> [--allow-expired]`** — writes the full record to a 0600 temp
  file and prints only the path (mirrors `resolve`'s tempfile discipline exactly). Refuses on
  an expired session unless `--allow-expired` is passed (surfaces `SessionExpired` as a clear
  CLI error otherwise).
- **`session remove <site> <account>`** — removes it, confirms by namespace only.

All five audit their action (a new gap this epic closes): `session_store`, `session_load`,
`session_inspect`... actually — audit only the state-changing/access actions
(`session_store`, `session_load`, `session_remove`), not read-only `inspect`/`list` (matching
the existing convention: `find`/`reg show` aren't audited either, only mutations and value
accesses are).

## 4. What Could Go Wrong

- **[high] `session load`'s temp file, or its printed path, ends up handled carelessly by a
  caller** (same risk `resolve` already carries and already mitigates the same way — not a new
  risk this epic introduces, but worth re-stating since it's the highest-stakes command here).
  Mitigation: identical 0600 discipline, identical "path only" contract, already proven in
  `resolve`'s existing tests.
- **[medium] `session store`'s value (a JSON blob, not a single string) needs to round-trip
  through stdin/file exactly like `drop`'s value does, but is structured, not a flat string.**
  Mitigation: read raw stdin/file text and `json.loads()` it before passing to
  `store_session()` — `store_session()` already validates JSON-serializability internally, so a
  malformed blob fails closed with a clear error, not a silent corrupt store.
- **[low] `session load` on an expired session with no `--allow-expired` should be a clear,
  actionable error, not a generic `BackendError` dump.** Mitigation: catch `SessionExpired`
  specifically before the generic `BackendError` catch, with a message naming the `--allow-
  expired` escape hatch.

## 5. Dependencies and Constraints

Depends only on already-shipped `localvault.py` methods (v0.4.0). No Registry/Broker
integration in this pass — sessions stay backend-only, consistent with today's design (the
original orphaned `portunus-session-vault` epic's role-scoped-gate idea remains a distinct,
larger, unstarted piece of work per that epic's reconciliation notes, not part of this).

## 6. Open Questions

None of real weight — this is a mechanical CLI-exposure pass over an already-designed,
already-tested library surface.

## 7. Verification Strategy

```
VERIFICATION PLAN:
  Tools: pytest, matching tests/test_cli_drop.py and tests/test_cli_audit_json.py conventions
  Automated: store/inspect/list/load/remove round-trip; load writes 0600 tempfile and never
    prints the payload; load refuses an expired session without --allow-expired with a clear
    message; store/load/remove write audit entries (inspect/list do not); malformed JSON on
    store fails closed
  Manual: none needed
  Not verifying: UI exposure (CLI only, this pass)
```

## 8. Scale Assessment

```
SCALE ASSESSMENT:
  Files affected: 2 (cli.py, a new test file)
  Subsystems: CLI only -- no changes to localvault.py, registry.py, broker.py, audit.py
  Migration required: no
  Unknowns: 0

  RECOMMENDATION: Proceed directly to stories (Small scope)
  RATIONALE: Every piece this epic needs already exists and is tested; this is purely wiring a
    CLI surface onto an already-correct library API, following patterns (drop's stdin
    discipline, resolve's tempfile discipline) already proven in this exact codebase.
```
