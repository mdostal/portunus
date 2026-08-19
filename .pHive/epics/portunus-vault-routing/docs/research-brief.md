# Research Brief — portunus-vault-routing

## Requirement

Fix the actual architectural gap behind "local only / cloud only / synced": today Portunus picks
exactly ONE backend for an entire process via a single global `PORTUNUS_BACKEND` env var
(`local` | `gcloud` | `aws` | `mock`) — a `Reference`'s own `provider`/`project` tags are pure
display metadata, never used to route which backend actually serves it. `backend.py`'s own module
docstring already claims backends are "selected per-Reference by provider+project" — that's
aspirational, not true yet. This is why every real-GCP command this session required manually
setting `PORTUNUS_BACKEND=gcloud` first; under the default (`local`), all 378 real GCP-backed
references simply fail to resolve.

User's confirmed direction (this conversation): build per-project vault routing (a "menu" per
project choosing local vault / GCP Secret Manager / future adapters), plus a **recency-aware,
pull-only** sync-down cache — GCP → local only, never local → GCP (explicit correction from an
initial "bidirectional" framing: "realistically, it would only sync down from the cloud, not the
other way around"). Motivating cases: (a) dev/agentic work wants flexible, mixed-backend access
across many small/varied secrets; (b) large-scale deploys (the user's own example: Kafka
clusters) want a `.env` materialized once at deploy time from a cached local copy rather than a
live Secret Manager fetch on every access. Scope confirmed via two AskUserQuestion rounds this
session: build the adapter/routing pattern for GCP + local only (real, working); AWS gets the
same stub treatment as today, updated to the new adapter interface; genuinely new third-party
adapters (Infisical, HashiCorp Vault, etc. — all named by the user as future "menu" options) are
explicitly OSS extension points for later, not built now. Key-rotation automation (auto-roll a
key via Vercel/GCP/etc APIs) is a stubbed, greyed-out UI placeholder only — no real functionality.

## What already exists

**`SecretBackend` (backend.py) is already a `Protocol`** with exactly one method,
`access(sm_name, project="") -> str`. Three implementations today: `LocalEncryptedBackend`
(localvault.py, has `store()` too), `GcloudBackend` (real, multi-project via `bindings` +
per-project WIF/account), `AWSSecretsManagerBackend` (interface-conformant stub, `access()`
always raises). `MockBackend` is the test double.

**`GcpProjectBinding`/`load_gcp_bindings`/`save_gcp_bindings`** (backend.py:61-127) is the
existing per-project config precedent this epic generalizes: a `PORTUNUS_HOME/gcp-bindings.json`
file, `{project: {wif_audience, account}}`, loaded into a `Dict[str, GcpProjectBinding]`, 0600 on
disk. `portunus bindings set/show` (cli.py) is the existing CLI surface. **This file is real,
already populated for the two real projects in production use this session**
(`demo-project-483920` → `personal@example.com`, `demo-cicd` → `work@example.com`) — any
format change here needs to keep reading the existing file correctly, not just a fresh-install
happy path.

**Backend selection today is 100% global, not per-reference**: `_build()` (cli.py:47-72) reads
`PORTUNUS_BACKEND` once and constructs exactly one backend instance for the whole process;
`Resolver.__init__(self, registry, backend, broker)` holds that single instance; `_fetch()`
always calls `self.backend.access(ref.sm_name, project=ref.project)` — the same backend object
regardless of which reference is being resolved. `mcp_server.py`'s tools call the same `_build()`.
There is currently no code path that picks a *different* backend class per reference.

**Recency signal for "has the remote value changed since we last synced it?"**: `gcloud secrets
versions describe latest --secret=<name> --project=<proj> --format=json` returns the *version's*
`createTime` (changes every time the value is rotated) — distinct from `discover.py`'s existing
`create_time` field, which comes from `gcloud secrets list` and is the *secret resource's*
creation time (doesn't change on rotation). No code in this repo calls `versions describe` today
— `discover.py` only calls `secrets list`, `GcloudBackend.access()` only calls
`secrets versions access latest` (which returns the value, not metadata). A recency check needs
a new, cheap, metadata-only gcloud call.

**UI**: Project Explorer (`ui/app/components/ProjectExplorer.tsx`) already renders a read-only
`wif_configured: boolean` badge per project (from `/api/discover`'s existing `wif_configured`
field) — the only existing per-project backend-config surface in the UI. No settings/menu
component exists yet anywhere in `ui/app/`.

## The real design questions

1. **Where does per-project backend choice live?** The existing `gcp-bindings.json` precedent is
   the natural home — extend its per-project schema with a `backend` field (default `"gcp"` for
   every existing entry, so the real, already-populated file for `demo-project-483920`/
   `demo-cicd` keeps working with zero migration) and a `sync_mode` field (default `"direct"` —
   today's live-fetch-every-time behavior, unchanged for every existing entry). This avoids a
   file-format migration entirely; old entries just get the new fields' defaults on read.

2. **How does the Resolver pick a backend per reference?** `Resolver` currently holds one fixed
   `backend`. The natural evolution: a small router that, given `ref.project`, looks up the
   binding and returns the right backend instance (constructing/caching `GcloudBackend` /
   `LocalEncryptedBackend` instances as needed) — falling back to today's single global
   `PORTUNUS_BACKEND`-selected backend when a reference's project has no binding (covers local-
   only refs with no `project` set, and any reference this doesn't apply to). This needs to be
   additive, not a breaking change to `Resolver`'s constructor signature, since both the CLI and
   MCP server construct `Resolver` via the shared `_build()`.

3. **What does "sync down" actually do, mechanically?** For a project bound with
   `sync_mode: "cached"`: on `access()`, call the cheap version-metadata check first; if the
   locally-cached copy's tracked version is missing or older than the remote's latest version,
   fetch the real value from the remote adapter, `LocalEncryptedBackend.store()` it, and record
   the new version marker; then always serve the read from the local encrypted copy. This needs
   somewhere to persist "last synced version per reference" — the cleanest fit is the local vault
   itself (already has 0600 file infrastructure) rather than a new field on `Reference` (which
   would put non-metadata state into the registry that gets published/browsed everywhere else).

## Validation confidence

Codebase-only for the routing/caching core (no new third-party library). The one new external
call is `gcloud secrets versions describe` — same `gcloud` CLI, same subprocess pattern already
used throughout `backend.py`/`discover.py`, no new SDK to validate.

## inconsistency_risk_signals

- Real risk: any change to `gcp-bindings.json`'s read path must be proven, not assumed, to still
  correctly load the actual production file for `demo-project-483920`/`demo-cicd` — this needs an
  explicit test against that exact existing shape, not just a fresh-fixture test.
- The module docstring in backend.py already asserts "selected per-Reference by provider+project"
  as if it were true today — grill should confirm the design doc doesn't just restate that
  aspiration without actually closing the gap between claim and code.
- Scale: this is materially larger than the two prior epics this session (portunus-mcp-server,
  portunus-local-create) — touches core resolution logic (`Resolver`), an existing production
  config file, a new caching mechanism, and a UI surface. Warrants presenting the design to the
  user for confirmation before story decomposition, not just grilling and proceeding silently.
