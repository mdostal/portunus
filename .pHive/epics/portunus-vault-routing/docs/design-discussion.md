# Design Discussion — portunus-vault-routing

## 1. Goal

Close the gap between what `backend.py`'s own docstring already claims ("selected per-Reference
by provider+project") and what the code actually does today (one global `PORTUNUS_BACKEND` env
var picks a single backend for the entire process). Concretely, per the user's confirmed
direction this session:

1. **Per-project vault routing** — a project's secrets resolve against whichever backend that
   project is bound to (local vault, GCP Secret Manager, ...), chosen via a real per-project
   "menu," not a process-wide env var.
2. **Recency-aware, pull-only sync-down caching** — a project can opt into `sync_mode: cached`:
   Portunus fetches from the remote backend, caches the value in the local encrypted vault, and
   only re-fetches when the remote's value has actually changed (compared by version recency) —
   never the other direction. This is the real, scoped-down form of "sync": explicitly NOT
   bidirectional, NOT a general replication engine — the user's own correction mid-conversation:
   "realistically, it would only sync down from the cloud, not the other way around."
3. **A real plug-and-play adapter shape**, proven with the two backends that already exist for
   real (local, GCP) plus the AWS stub updated to the same shape — not a new third-party
   integration. Infisical/HashiCorp/etc. are named future menu options, deliberately out of scope
   here; the deliverable is an interface future adapters can implement without touching core
   resolution logic, matching the user's own framing: "this needs to be plug and play with
   adapters, not hard locked."

Explicitly **out of scope**, confirmed by the user this session: real key-rotation automation
(a UI stub/greyed-out placeholder only — "stub it out for now and grey it out in UI but it's
coming"); any local→cloud push; building real Infisical/HashiCorp adapters ("someone else can do
that if they care or they can ask for it").

## 2. Why this is scoped as Large, not Medium

Unlike this session's two prior epics (2-3 small, additive tools each), this one touches: (a) an
already-in-production config file (`gcp-bindings.json`, real content for `personalsites-487021`/
`ffe-cicd`), (b) `Resolver`'s core resolution path (currently one fixed backend per instance,
used by every CLI command and all 10 MCP tools), and (c) a new caching mechanism with real
correctness requirements (never serve a stale value past its recency window; never silently drop
a sync failure into "looks fine"). Presenting this design for confirmation before writing stories
is the right gate here, not a formality — a wrong turn in the migration story specifically risks
the real vault's existing configuration.

## 3. Proposed approach

### Slice A — `VaultBinding` model + migration-safe config

Rename `GcpProjectBinding` → `VaultBinding` (backend.py), adding two fields:
`backend: str = "gcp"` (one of `local | gcp | aws`) and `sync_mode: str = "direct"` (one of
`direct | cached`). `wif_audience`/`account` stay as GCP-specific optional fields (ignored by
other backend types).

`VaultBinding.backend` and `Reference.provider` (existing, free-text tag since portunus-vault-
metadata) answer different questions and must not be conflated (grill H4): `provider` is *who
issued* the secret — a Vercel-issued key can perfectly well be stored in GCP Secret Manager, so
`provider="vercel"` on a reference in a project bound to `backend="gcp"` is the normal case, not
a contradiction. `VaultBinding.backend` is *where the value physically lives* — which adapter
Portunus actually calls to fetch it. Rename `load_gcp_bindings`/`save_gcp_bindings` → `load_vault_bindings`/
`save_vault_bindings` — a clean rename across every call site (`backend.py`'s own
`GcloudBackend.__init__`, `cli.py`'s `_build`/`cmd_bindings_set`/`cmd_bindings_show`/
`cmd_auth_gcp`/`cmd_discover`/`_wif_configured`, `mcp_server.py`'s `portunus_bindings_show`/
`portunus_discover`), not a back-compat shim — this is a controlled, one-epic refactor with every
call site known in advance.

**Migration, made safe by construction, not by a migration script:**
- `save_vault_bindings()` always writes to a **new** file, `PORTUNUS_HOME/vault-bindings.json`.
- `load_vault_bindings()`: if `vault-bindings.json` exists, read it (new schema, `backend`/
  `sync_mode` included). Else if the legacy `gcp-bindings.json` exists, read it under the OLD
  schema and default every entry to `backend="gcp", sync_mode="direct"` — **byte-for-byte the
  same effective behavior the real vault has today**, just expressed through the new model. Else
  fall back to today's env-var-derived single-binding default (unchanged).
- The legacy file is never written to, moved, or deleted by this epic. The real
  `personalsites-487021`/`ffe-cicd` bindings keep working with zero manual migration step; the
  new file is created automatically the first time anyone calls `portunus bindings set` again
  (any field), naturally forward-migrating.
- **Acceptance-critical test**: load the *actual* current `gcp-bindings.json` shape (verbatim,
  not a simplified fixture) through the new loader and confirm both real projects still resolve
  to `backend="gcp"` with their existing `account`/`wif_audience` values intact.

### Slice B — Backend router (the actual "per-project, not one global choice" fix)

`Resolver.__init__` gains an optional `backend_for: Optional[Callable[[Reference], SecretBackend]]
= None` parameter. When `None` (every existing call site, every existing test), behavior is
byte-identical to today — `_fetch()` still calls `self.backend.access(...)`. When provided,
`_fetch()` calls `self._backend_for(ref).access(ref.sm_name, project=ref.project)` instead. This
is strictly additive — no existing test, no existing `Resolver(registry, backend, broker)` call
site changes.

`_build()` (cli.py) constructs the real router: a small closure/class that, given a `Reference`,
looks up `vault_bindings.get(ref.project)`. If found, dispatches on `.backend` (`local` →
`LocalEncryptedBackend()`, `gcp` → `GcloudBackend(...)` configured from that binding, `aws` →
`AWSSecretsManagerBackend()`) — caching one instance per backend type per `Resolver` lifetime, not
reconstructing per call. If no binding matches `ref.project` (or the reference has no `project`
set at all — e.g. every reference created via this session's `portunus_drop`), falls back to the
single globally-selected backend from today's `PORTUNUS_BACKEND` env var, **unchanged**. This
means: a project with a real vault binding routes automatically; everything else behaves exactly
as it does today. No existing reference stops working; `PORTUNUS_BACKEND=gcloud` is no longer
*required* for the two real bound projects, but nothing breaks if someone keeps setting it.

**`PORTUNUS_BACKEND=mock` always short-circuits the router entirely** (grill H2) — `_build()`
only constructs the router when `PORTUNUS_BACKEND` is unset or one of today's real modes
(`local`/`gcloud`/`aws`). Under `mock`, every reference resolves through the single `MockBackend`
exactly as today, regardless of any `vault-bindings.json` content — a stray bindings file must
never cause a test or dry run to reach for a real `GcloudBackend`. This is a safety rail, not an
edge case: this session's ~300 existing tests all run under `mock`, and none of them should ever
become capable of constructing a real backend by accident.

### Slice C — Recency-aware sync-down cache

New `GcloudBackend.latest_version(sm_name, project="") -> str` method: calls `gcloud secrets
versions describe latest --secret=<sm_name> --project=<project> --format=json`, returns the
version's `createTime` as the recency marker (changes on every rotation, unlike the secret
resource's own `createTime` that `discover.py` already reads). Cheap, metadata-only — no value
ever touches this call.

New `SyncingBackend` (backend.py): wraps a `remote: SecretBackend` + `local: LocalEncryptedBackend`
+ a small `SyncState` store (`PORTUNUS_HOME/sync-state.json`, 0600, `{key: last_synced_marker}`
— deliberately NOT a `Reference` field; this is cache-invalidation bookkeeping, not registry
metadata that should show up in `portunus list`/the UI's metadata views). `access(sm_name,
project)`:
1. If `remote` doesn't implement `latest_version` (only `GcloudBackend` does, this epic), always
   fetch live from `remote` and cache the result — correct, just not optimally cached; keeps the
   wrapper generic for future adapters that don't support a cheap recency check yet.
2. Else, call `remote.latest_version(sm_name, project)`. If it matches the stored marker AND a
   local copy exists, serve from the local cache — no remote value-fetch at all.
3. Else (stale or first sync), fetch the real value from `remote.access(...)`, cache it locally,
   update the marker, then serve it.

**Local-store key is `f"{project}:{sm_name}"`, not bare `sm_name`** (grill H1). `portunus_drop`
and `SyncingBackend` both ultimately write into `LocalEncryptedBackend`'s single flat store — a
cached GCP secret and a genuinely-local secret that happen to share an `sm_name` would otherwise
silently collide (whichever writes last wins, no error). Project-prefixing the cache key removes
that collision for every realistic case without requiring `portunus_drop` itself to change.

**Cached copies get no new at-rest exposure** (grill H3): `SyncingBackend` calls
`LocalEncryptedBackend.store()`/reads unchanged — a cached remote value is Fernet-encrypted at
rest exactly like a directly-`portunus_drop`-ped secret. This introduces no new plaintext-at-rest
location.

A project binding gets this wrapper instead of a bare `GcloudBackend` when `sync_mode="cached"` —
the router (Slice B) constructs `SyncingBackend(GcloudBackend(...), LocalEncryptedBackend(), ...)`
in that case, `GcloudBackend(...)` directly when `sync_mode="direct"` (today's behavior, and the
default for every existing binding per Slice A's migration).

### Slice D — CLI/MCP surface

- `portunus bindings set <project> --backend {local,gcp,aws} --sync-mode {direct,cached}` — new
  flags alongside the existing `--account`/`--wif-audience`.
- `portunus bindings show` / `portunus_bindings_show` (MCP) — extended to report `backend`/
  `sync_mode` per project (still never the WIF audience value itself, matching the CLI's existing
  restraint).
- `portunus sync <project>` / a new `portunus_sync(project)` MCP tool — an **explicit** trigger
  that forces a recency check (and re-fetch if stale) for every `cached`-mode reference in that
  project, ahead of relying on incidental `access()` calls. Motivated directly by the user's own
  deploy scenario: materialize a `.env` once at deploy time from a guaranteed-fresh local cache,
  not a live Secret Manager round-trip per secret per instance. Returns a metadata-only report
  (`{"synced": [...], "already_fresh": [...], "failed": [...]}` — names only, never values).

### Slice E — UI: per-project vault menu + rotate-key stub

Project Explorer's existing read-only `wif_configured` badge becomes a real per-project settings
control: a `backend` dropdown (Local / GCP / AWS — AWS visibly marked "not yet implemented," same
restraint as the CLI stub) and a `sync_mode` toggle (Direct / Cached), backed by a new
`/api/bindings` route (thin shell-out to `portunus bindings set/show`, same pattern every other
route already uses — no gating logic duplicated in TypeScript). A disabled, greyed-out "Rotate
key" button on the reference DetailDrawer — visual placeholder only, no route, no handler, a
tooltip along the lines of "coming soon" — signals the future key-rotation direction without
building it.

### Slice F — Closeout

Docs (README's MCP/CLI sections, `.pHive/CONTEXT.md`'s ARCA terminology entry — this is exactly
what that entry should have described from the start), version bump (this changes core resolution
behavior — **minor**, not major: additive/backward-compatible per Slices A/B's own migration
guarantees, no existing caller breaks). Live proof against the real vault: bind one real project
(likely `personalsites-487021`, the smaller of the two) to `sync_mode="cached"`, run
`portunus sync personalsites-487021`, confirm a real local encrypted cache gets created, confirm a
second sync run reports `already_fresh` (no redundant GCP fetch) via the real `latest_version`
check, and confirm `ffe-cicd` (left at `sync_mode="direct"`, untouched) still resolves exactly as
it does today through the legacy-file fallback path.

## 4. What "the secure store was supposed to be a package" means here

The user recalled (uncertain themselves — "pretty sure there were good options we found before")
earlier research into a third-party Python secret-storage library as a possible foundation for
the local vault tier. No trace of that research exists in this session's memory or in the
codebase's history/comments. Rather than guess or block on it, this epic treats it as a genuinely
open, non-blocking item: `LocalEncryptedBackend`'s current implementation (`cryptography`'s
Fernet recipe, already reviewed and shipped) is untouched by this epic, and evaluating a
replacement library is explicitly deferred — worth a short, separate research pass later, not a
prerequisite for per-project routing or sync-down caching to ship.

## 5. Risks

| Risk | Severity | Mitigation |
|---|---|---|
| A bug in the new config loader silently breaks the real `personalsites-487021`/`ffe-cicd` bindings | Critical | Slice A's acceptance criteria require testing against the *actual* current `gcp-bindings.json` content, not a simplified fixture; the legacy file is never written/moved, only read; new file is additive |
| `SyncingBackend` serves a stale cached value past a real rotation, and a caller can't tell it's stale | High | Recency check runs on every `access()` call by design (Slice C step 2) — staleness is structurally impossible to miss for any `cached`-mode reference; the explicit `portunus sync` trigger (Slice D) exists specifically for callers who want a guaranteed-fresh point-in-time refresh (the deploy use case) rather than relying on next-access timing |
| Router silently mis-routes a reference to the wrong backend | High | Fallback-to-today's-global-backend behavior only fires when no binding matches `ref.project` — never a *wrong* binding; every routed case is an explicit, human-configured entry in `vault-bindings.json` |
| Scope creep into real key rotation or bidirectional sync | Medium | Explicitly out of scope per §1, confirmed twice by the user this session; the UI's rotate-key control is inert by construction (no route/handler exists to accidentally wire up) |

## 6. Open questions

None blocking. §4's "secure store package" note is the one deliberately deferred, non-blocking
item.

## 7. Scale assessment

**Large.** Touches core resolution logic (`Resolver`), an existing production config file, a new
caching subsystem with real correctness requirements, CLI + MCP + UI surfaces, and a version
bump. Six slices, likely 6-7 stories. Presenting this design to the user for confirmation before
story decomposition, per the size and the real-data migration risk in Slice A.
