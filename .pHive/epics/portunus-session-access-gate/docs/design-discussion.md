# Design discussion: portunus-session-access-gate

## 1. The gate: a synthetic in-memory Reference, not a new mechanism

`Broker` gains `check_session_access(site, account, *, org="", project="", env="", tags=None,
requester=None)`. It constructs a `Reference` (the real dataclass from registry.py — not a new
type) with `name=f"session:{site}:{account}"`, `sm_name=""`, and whatever scope metadata the
session's own record carries, purely in-memory, **never persisted to the Registry**. That object
is handed straight to `roles.evaluate(roles.load_policies(), requester, synthetic_ref)` — the
exact same call `check_injectable()` already makes. Same audit discipline (a `would-allow`/
`would-deny` line every time `requester` is real), same enforcement gate (`NotAuthorized` only
when `roles.enforcement_is_on()`), same permissive-if-unconfigured default.

**Self-grill: why not make a session a real Registry `Reference`?** It isn't a secret in the same
shape — no `sm_name` pointing at a backend-stored value, no `state`/`approval` lifecycle, a
different TTL model. Forcing it into the Registry would surface sessions in `reg show`/Console/
Project Explorer where they don't belong and would need every one of those surfaces to learn to
treat this one reference "kind" differently. A synthetic, throwaway `Reference` costs nothing and
`roles.evaluate()` already accepts it unchanged — `_scope_matches()` is fully duck-typed
(`getattr(ref, policy.scope_type, "")`), confirmed by reading it directly, not assumed.

## 2. Where the scope metadata comes from

`store_session()` gains optional `org`/`project`/`env`/`tags` kwargs, stored as a new `"scope"`
key in the existing record shape (alongside `namespace`/`ttl`/`rotation`/`session`) — additive,
no schema version bump needed (`SESSION_SCHEMA` unchanged; an old record simply has no `scope`
key, treated as all-empty, matching "no policy configured" -> always-allow). `_session_view()`
projects `scope` out alongside the existing metadata fields — still never the payload.
`cmd_session_load` reads it via the existing `inspect_session()` call (metadata-only, already
safe to call before deciding whether to grant load access) and passes it to
`check_session_access()` before ever calling `load_session()` for real.

## 3. Only the fetch boundary is gated — mirrors `check_injectable`'s own precedent exactly

`session store`/`remove`/`inspect`/`list` stay ungated. This isn't an oversight — it's the same
line `check_injectable()` already draws: writes (`drop`, `retag`, `state`) and metadata-only
views (`reg show`, `list`) are never policy-gated today, only the actual fetch/injection
boundary (`resolve`) is. Gating `session load` and nothing else keeps this consistent rather than
inventing a broader (or narrower) gate surface for sessions specifically.

## 4. The Playwright question, resolved (research-brief.md §3)

No new production module. `browser.new_context(storage_state=<path>)` already accepts the exact
path `session load` already prints. "Story 05" becomes a real integration test:
`tests/test_session_playwright_integration.py`, guarded by `pytest.importorskip("playwright")`
so the core suite never requires the (large, browser-binary-downloading) `playwright` package —
skipped gracefully when it isn't installed, run for real when it is. The test: store a realistic
`storageState`-shaped session via `store_session()`, load it via the real CLI (`portunus session
load`), feed the printed path straight into a real `sync_playwright()` ->
`browser.new_context(storage_state=path)` call, and confirm the resulting context's cookies match
what was stored — proof the already-existing mechanism genuinely works for its intended
consumer, not a synthetic assertion about JSON shape.

**Self-grill: is `playwright` a new dependency?** Only a *test-time*, optional one — never added
to `pyproject.toml`'s core `dependencies`. `importorskip` means CI/a fresh clone's test run never
needs it; the live proof for this epic's own closeout installs it once, for real, to generate
genuine evidence, matching this session's own "live proof over synthetic fixture" discipline
throughout.

## 5. Version bump

`minor` — a new `Broker` method + optional CLI flags + a new gated code path, all additive;
nothing existing changes shape (an unconfigured vault's `session load` behavior is unchanged,
matching `check_injectable`'s own permissive-if-unconfigured default exactly).
