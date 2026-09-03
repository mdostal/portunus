# Research brief: portunus-session-access-gate

## 1. Origin

Follow-on to GitHub issue #136, itself the product of a branch-cleanup audit (2026-09-02) that
found two never-merged branches (`feat/PAN-7807`, `feat/PAN-7835`) containing real, unshipped
work: a role-scoped session-access gate, and a Playwright injection helper. This epic re-scopes
that work against the *current* codebase — `.pHive/epics/portunus-session-vault/epic.yaml`'s own
Stories 03 ("Ostiarius role-scoped gate") and 05 ("Playwright integration tests"), confirmed by
that epic's own 2026-08-19 reconciliation note to be the two genuinely-unstarted stories.

## 2. What already exists — confirmed directly

`portunus session store/load/inspect/list/remove` (localvault.py + cli.py) already work: TTL
enforcement (`SessionExpired`), `list_sessions()`, boundary-safe retrieval (`cmd_session_load`
writes a 0600 tempfile and prints only the path — the exact same discipline `resolve` has).
**Confirmed gap**: `store_session()`/`load_session()` are called directly, bypassing
`Broker.check_injectable()`/`roles.evaluate()`/the audit chain's policy-decision entries
entirely — a session is exactly as sensitive as a secret value and currently gets none of the
scope-gating a regular reference does.

`portunus-petitio-rbac` (shipped since these branches were written) added `roles.evaluate(
policies, requester, ref) -> Decision` — the single seam every scope/precedence decision in this
codebase now goes through. Critically, confirmed by reading `_scope_matches()` directly: it's
fully duck-typed (`getattr(ref, policy.scope_type, "")`) — `ref` doesn't need to be a real
Registry `Reference`, only an object with matching attribute names. This means session access
can be gated through the *exact same* `roles.evaluate()` function without inventing a second
gating mechanism, and without a session needing to become a real Registry entry.

## 3. The Playwright question — resolved by checking the actual API, not assumed

The old branches (`feat/PAN-7807`, `feat/PAN-7835`) each built a bespoke `playwright.py` module:
load a session, gate it, write to a 0600 tempfile, yield the path for the caller to hand to
Playwright. **Verified directly against Playwright's own documented API**: `browser.new_context(
storage_state=...)` already accepts *either* a dict *or* a file path string pointing at a JSON
file. `cmd_session_load` already does exactly that — writes the full session record to a 0600
tempfile and prints only the path.

**This means the "Playwright injection helper" gap doesn't actually exist as a missing piece of
production code.** `portunus session load <site> <account>` already produces exactly what
`browser.new_context(storage_state=<printed-path>)` needs — no new module required. What's
genuinely missing is *proof* that this already-existing mechanism actually works against a real
Playwright browser context, which is what "Story 05" should become: a real integration test, not
new production code. This directly resolves the open question the `portunus-session-vault`
epic's own reconciliation note left unanswered ("Story 05's Playwright-specific framing may
itself be stale scope... the real shipped capability is a generic login/browser-session store").

## 4. Scope boundary

In scope: gating `session load` (the one payload-exposure boundary) through `roles.evaluate()`,
mirroring `check_injectable()`'s own precedent exactly (would-allow/would-deny audit line always;
raises `NotAuthorized` only when `roles.enforcement_is_on()`); optional `--org`/`--project`/
`--env`/`--tags` scope metadata on `session store` for `roles.evaluate()` to match against; a
real Playwright integration test proving the existing `session load` -> tempfile-path ->
`browser.new_context(storage_state=path)` chain genuinely works.

Explicitly NOT in scope: gating `session store`/`remove`/`inspect`/`list` (mirrors
`check_injectable`'s own precedent — only the fetch/injection boundary is gated, never writes or
metadata-only views, matching how `drop`/`retag`/`reg show` are never gated either); a new
Playwright-specific production module (§3 — not needed); making a session a real Registry
`Reference` (the synthetic in-memory `Reference` `roles.evaluate()` already accepts is
sufficient, and keeps sessions out of `reg show`/Console/Project Explorer, which is correct —
they're not secrets in that same shape).
