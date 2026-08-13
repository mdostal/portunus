# Design Discussion: portunus-session-ttl-and-list

## 1. What Are We Doing?

Fixing a real correctness gap found during `portunus-standalone-core`'s closeout reconciliation
(`.pHive/epics/portunus-session-vault/epic.yaml`): `LocalEncryptedBackend.load_session()` never
checks its own `ttl.expires_at` metadata — an expired session is returned exactly like a valid
one. We're also adding `list_sessions()`, since there's currently no way to enumerate what's
stored without knowing every site/account pair in advance. Both ship in this repo's actual
Python source (`localvault.py`), not the orphaned TypeScript-planned epic.

## 2. What I Found

`store_session()` already computes and stores `ttl.expires_at` (ISO 8601, UTC) correctly.
`load_session()` decrypts and returns the full record with zero expiry check. `inspect_session()`
calls `load_session()` for its metadata view — so fixing expiry checking in `load_session()`
would also make `inspect_session()` refuse on an expired session, which is wrong: a human
*should* be able to see that a session is expired (that's the whole point of surfacing TTL
metadata). So the fix needs an opt-out for metadata-only callers, not a blanket raise.

No `list_sessions()` exists; the vault's raw storage (`self._load()`) is a flat
`sm_name -> encrypted_value` dict, and session keys are already namespaced as
`session:<site>:<account>` (URL-quoted) — filterable by prefix without any new storage format.

## 3. My Proposed Approach

**Slice 1 — TTL enforcement.** `load_session()` gains `allow_expired: bool = False`; when the
computed expiry has passed and `allow_expired` is `False`, raise a new `SessionExpired`
(subclasses `BackendError`, so existing `except BackendError` callers don't need to change).
`inspect_session()` calls `load_session(..., allow_expired=True)` and adds an `expired: bool`
field to its metadata view (a real UX improvement that falls out of the fix almost for free).

**Slice 2 — list_sessions().** Refactor the metadata-shaping logic out of `inspect_session()`
into a shared `_session_view(record)` helper; `list_sessions()` iterates the vault's raw keys,
filters to the `session:` prefix, decrypts+parses each, and returns `_session_view()` for all
of them (including expired ones, same non-raising posture as `inspect_session`).

## 4. What Could Go Wrong

- **[medium] Clock skew / timezone bugs in the expiry comparison.** Mitigation: `_utc_now()`
  and `expires_at` are both already UTC-aware ISO 8601; reuse the exact same comparison
  primitives rather than introducing a new date-parsing path.
- **[low] `list_sessions()` silently swallows a corrupt/undecryptable session entry.**
  Mitigation: acceptable and intentional — one corrupt entry shouldn't break enumeration of
  everything else; this mirrors `_load()`'s existing graceful-degradation posture on a corrupt
  vault file.

## 5. Dependencies and Constraints

Both slices touch only `localvault.py` — no registry/CLI/UI changes needed for this pass (CLI
exposure for sessions remains a separate, larger gap per the session-vault epic's
reconciliation notes — not part of this scope).

## 6. Open Questions

None — this is a small, contained bug-fix-plus-enumeration pass with no real ambiguity.

## 7. Verification Strategy

```
VERIFICATION PLAN:
  Tools: pytest (existing tests/test_localvault.py conventions)
  Automated: expired session raises SessionExpired by default, allow_expired=True bypasses it,
    inspect_session never raises on expired (shows expired:true instead), list_sessions
    enumerates all sessions with correct expired flags and skips corrupt entries
  Manual: none needed
  Not verifying: CLI/UI exposure (out of scope, no CLI session commands exist yet)
```

## 8. Scale Assessment

```
SCALE ASSESSMENT:
  Files affected: 2 (localvault.py, tests/test_localvault.py)
  Subsystems: ARCA local-encrypted backend only
  Migration required: no
  Unknowns: 0

  RECOMMENDATION: Proceed directly to stories (Small scope)
  RATIONALE: Single-file bug fix + one new enumeration method, no new subsystems, no UI/CLI
    surface, fully covered by existing test conventions in test_localvault.py.
```
