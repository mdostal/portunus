# Changelog

All notable changes to Portunus are documented in this file.

## [Unreleased]

## [0.18.0] - 2026-08-14

### Added

- **`discover --register` eagerly warms the local cache** (CLI + MCP) for a project bound
  `sync_mode=cached` — collapses "add a key to GCP Secret Manager" from a multi-step dance
  (register, then wait on the first real resolve to trigger the cache pull) down to one step.
  The reference's `state` still lands at `requested`: it's cached sooner, not injectable sooner
  — `check_injectable` still gates every real resolve/inject/ask/MCP path exactly as before.
  Per-reference, best-effort: a cache-warm failure never fails registration itself.
- **`portunus vault export`/`portunus vault import`** — a coordinated, passphrase-locked
  portable snapshot of the vault's critical-state surface (reference registry, master key,
  encrypted values, vault bindings, legacy bindings if present, audit log + its sequence
  counter). Re-encrypted under an operator passphrase (PBKDF2-SHA256, 600k iterations) — the
  archive never carries the vault's own live decryption key. `import` fails closed on a wrong
  passphrase and refuses to touch an existing non-empty vault without `--force` (a full
  replace, never a merge). CLI-only: no MCP tool, no UI surface — an archive containing every
  secret in the vault should never be triggerable by an LLM-facing tool without a human
  directly running the command.

### Fixed

- **`vault-bindings.json` had no lock at all** — the only critical-state file without one,
  unlike the registry/audit-log/local-vault (each already fixed for a real concurrency bug
  earlier this cycle). `save_vault_bindings()` now serializes its write through a dedicated
  flock, and the new coordinated multi-file snapshot primitive backing `vault export` acquires
  every relevant lock (registry, vault, vault-bindings, audit) in one fixed order before
  reading, so an export is never caught mid-mutation.
- **Audit chain restore hazard, caught by the export/import round-trip tests themselves**:
  `.clock` (the append() sequence counter) wasn't included in what a snapshot captured —
  restoring `audit.log` without it would silently reset the counter, letting the next real
  append re-mint a `seq` that already exists in the restored chain and break the hash-chain
  invariant `verify()` checks. `.clock` now travels with `audit.log` under the same lock.

## [0.17.0] - 2026-08-15

### Added

- **`repo` + `source_files` reference metadata.** Found by inspecting the real data first: all
  342 `ffe-cicd` references share one GCP project across many repos/services, with nothing
  distinguishing *which git repo* actually consumes a secret. `repo` is a new structured field
  (`find --tags repo=...` works immediately, same as provider/project/env); `source_files` is a
  list of file paths in that repo, same optional posture as `related`.
- **`portunus retag-bulk --group-prefix <p> [--repo ...] [--source-files ...] [--dry-run]`** —
  backfills every reference whose `group` starts with a prefix in one command instead of one
  `retag` per reference. `--dry-run` previews with zero writes; one reference's collision
  failure never aborts the rest of the batch (same precedent `drop-bulk` set).
- **`portunus tree --by {group,repo}`** (CLI + MCP) — the same tree render, keyed on the
  structured `repo` field instead of the free-text `group` path. A reference with no `repo`
  lands under a `(no repo set)` bucket, same non-dropping guarantee `(ungrouped)` already has.
  Project Explorer's UI tab gets a matching Group/Repo toggle.
- **`related` renders as clickable chips** in the UI's detail view instead of plain text —
  clicking one switches straight to that reference. A relationship-graph *visualization* was
  explicitly considered and deferred: `related` has 2 real data points in the whole vault today,
  not yet a substrate worth a graph renderer.

Live-verified against the real `ffe-cicd` data (384 references in the vault at the time): `retag-bulk
--group-prefix ffe-cicd/event-api --repo event-api --dry-run` correctly identified 91
references with zero collisions and made zero writes. The actual backfill against real
production data is an explicit follow-up for whoever owns that vault to confirm, not something
this release does unsupervised — the real data's repo naming is a judgment call, not something
to guess at automatically.

## [0.16.3] - 2026-08-15

### Fixed

- **Local vault: concurrent `store()`/`remove()` could silently lose writes.** Found live from
  a real bug report — a different session resolving a shared, `sync_mode=cached` reference saw
  Portunus serve a stale value while other sessions saw it working fine. Verified directly
  first (hashed Portunus's served value against a fresh direct GCP fetch — identical, ruling
  out an ongoing bad value), which pointed at a timing-dependent race rather than a static bug.
  `LocalEncryptedBackend.store()`'s load-mutate-flush was unlocked — the same class of bug just
  fixed in the audit chain (0.16.2), but here governing actual secret values in a codepath
  (`SyncingBackend`'s cache refresh) multiple concurrent sessions hit by design. Reproduced
  directly: 20 concurrent `store()` calls lost 14 of 20 writes and crashed twice on a shared
  temp filename. Fixed with the same `flock` idiom `Registry`/`AuditChain` already use.

## [0.16.2] - 2026-08-15

### Fixed

- **Audit chain: a concurrency race could break the hash chain.** Found on the real
  production vault — `portunus verify` reported BROKEN. `AuditChain.append()`'s
  sequence-counter increment and its read of the prior entry's hash were both unlocked; two
  processes appending close together (two `portunus resolve` calls, or a race against the MCP
  server handling a concurrent request) could read the same "last" state before either wrote,
  producing a duplicate `seq` and an unrecoverable break. Fixed with the same `flock` idiom
  `Registry` already uses. Does not retroactively repair an already-broken historical chain —
  see the fix's own notes for why that's a separate, explicit decision, not something to do
  silently.

## [0.16.1] - 2026-08-14

### Fixed

- **Desktop app: sidecar-spawned `portunus` CLI calls could hang indefinitely.** Found by
  actually installing and running the app against the real vault instead of only scratch
  vaults. The spawned CLI subprocess's working directory defaulted to wherever the sidecar
  process happened to be running from — inside the installed app, that's the bundled Next.js
  build's own `node_modules` tree, and Python's import system treats the working directory as
  an implicit module search path, so a filesystem scan over that large bundled tree could hang.
  Fixed by pinning the subprocess's working directory explicitly instead of inheriting it.

## [0.16.0] - 2026-08-14

### Added

- **Tauri desktop app (macOS).** A native menu-bar shell around the existing Next.js Vault UI
  (`ui/src-tauri/`) — no more `cd ui && npm run dev` every time; `npm run dev` stays fully
  valid alongside it. Wraps the UI's existing `output: "standalone"` build as a sidecar,
  capturing the real login-shell PATH before spawning it (GUI-launched processes get a
  near-empty one, confirmed live, not assumed) and always picking a fresh free port (never a
  hardcoded one). Tray icon: Open Vault / Check for Updates… / Launch at Login / Quit —
  closing the window hides it (sidecar keeps running); Quit explicitly kills the sidecar,
  verified against the actual Apple Event quit path (Cmd+Q/Dock), not just a raw signal.
  Single-instance enforced — a second launch focuses the existing window.
- **Self-update, without an embedded credential.** Checks this (private) repo's latest GitHub
  release via the user's own already-authenticated `gh` CLI — never a token baked into the
  shipped app, the exact credential-in-a-binary anti-pattern this project exists to prevent.
  Always asks before installing (a native dialog, never a silent unattended swap). The actual
  swap is a detached relauncher script with a required, tested failure path: verifies the new
  bundle (Info.plist + codesign) before touching anything, backs up the original during the
  swap, and restores it on any failure — verified live with a deliberately corrupted download
  (rejected cleanly, original untouched) and a simulated mid-swap failure (original correctly
  restored from backup).
- Ad-hoc signed (macOS Apple Silicon) — a deliberate v1 scope decision for a single-user,
  single-machine tool, not a gap; full Apple notarization is a separable follow-up only needed
  for public distribution. `.github/workflows/release-desktop.yml` builds and attaches the
  `.app` to the same `vX.Y.Z` release this project's Python package already ships under — one
  version number, one release, no second parallel versioning scheme.

## [0.15.0] - 2026-08-14

### Added

- **Unified metadata form.** `AddSecretForm` (the UI's create-time form) now exposes
  `kind`/`scope`/`backend` plus `description`/`purpose`/`injected_as`/`group`/`related` —
  the full field set `DetailDrawer`'s edit-time Move form already had, all wired through the
  existing `/api/drop` → `portunus drop` path. No new backend concepts; every field but one
  already had full CLI/route support.
- **`portunus drop --backend`.** Closes a confirmed, small gap: `portunus_drop`'s MCP tool and
  `drop-bulk`'s per-entry `backend` already let a caller override which backend a single
  reference uses; the CLI's one-off `drop` command didn't. Now it does, matching
  `Reference.backend`/`Registry.add`'s existing kwarg exactly.
- **Rotation provenance.** New `rotation.py`: `RotationBinding` (provider/status/account),
  keyed by provider like `VaultBinding` is keyed by project, persisted at
  `PORTUNUS_HOME/rotation-bindings.json`. Three stub `RotationAdapter`s (Vercel — the
  confirmed priority target for the first real one — GitHub, Stripe), each `.rotate()`
  unconditionally raising, matching every ARCA stub backend's own restraint. CLI
  `rotation-bindings set/show`, MCP `portunus_rotation_status` — metadata only, never a
  credential. `DetailDrawer`'s "Auto-rotate…" button (inert since v0.14.0) is now wired to
  this registry's derived real/stub state instead of a hardcoded `disabled` attribute —
  behaviorally unchanged today (every provider is still a stub) but structurally real, and
  verified live in-browser to flip both directions when a binding's status changes.
- **`docs/architecture.md` §5** — rotation provenance, including the recursive design decision
  that a future real adapter would authenticate using its own admin credential, itself just
  another Portunus-managed `Reference` resolved through the same boundary-only
  `resolver.resolve_call()` every other value uses. Aspirational — zero real adapters exist yet.

## [0.14.0] - 2026-08-14

### Added

- **Five honest ARCA stubs** — HashiCorp Vault/OpenBao, Infisical, Doppler, 1Password Secrets
  Automation, Azure Key Vault — each interface-conformant and fails closed with a clear error
  and a link to `.github/ISSUE_TEMPLATE/adapter-request.yaml`, matching
  `AWSSecretsManagerBackend`'s existing restraint. Local + GCP remain the only real backends,
  per a 10-agent research workflow where all six candidates independently recommended
  stub-only (no validated environment to build a real adapter against yet).
- **Offline-resilient sync-down.** `SyncingBackend` now falls back to the last-known-good local
  copy (`last_sync_result="stale-offline"`) when a real connectivity failure interrupts its
  recency check, instead of hard-failing — without ever marking that copy as verified-fresh.
  This is what actually makes a `sync_mode="cached"` project keep working while disconnected.
- **Petitio's first seam.** `Identity` (name + kind: human/agent/system) and an optional,
  deliberately inert `requester` parameter on `Broker.check_injectable` — every caller is
  currently allowed regardless of `requester`. Real role-based enforcement (a policy store, an
  escalation-request flow modeled on Teleport's request→review→time-boxed-grant pattern,
  researched and documented) is designed but intentionally not built this release.
- **UI: two-zone backend picker.** Real backends (Local, GCP) and honest stubs never share a
  click target — selecting a stub opens an explanatory modal and a pre-filled GitHub
  adapter-request link instead of a config flow, per OSS adapter-marketplace research (Grafana/
  Airbyte/dbt/Terraform Registry precedent).
- **`docs/architecture.md`** — this repo's first adopter-facing architecture reference, with
  four diagrams: the component model, ARCA's backend-selection precedence, Petitio today vs.
  the designed future, and the request/resolve sequence.
- New `.github/ISSUE_TEMPLATE/adapter-request.yaml` for requesting a new ARCA backend.

## [0.13.1] - 2026-08-14

### Added

- Two new Claude Code skills, installed both in-repo and at Claude Code's user scope alongside
  the existing `portunus-ask`: `portunus-drop` (store a secret you were just handed, single or
  bulk, local-vault only) and `portunus-vault-setup` (configure/check a project's backend +
  sync mode, force a sync). All three now cross-reference each other so an agent lands on the
  right one regardless of which it reaches for first.

## [0.13.0] - 2026-08-14

### Added

- **Per-project/per-reference vault routing** — closes the gap between `backend.py`'s own
  long-standing docstring claim ("selected per-Reference by provider+project") and reality (one
  global `PORTUNUS_BACKEND` env var per process). `Resolver` now routes each reference through a
  3-level precedence: the reference's own `backend` override, else its project's
  `VaultBinding.backend`, else the global `PORTUNUS_BACKEND`-selected backend as the final,
  unchanged fallback. `PORTUNUS_BACKEND=mock` always short-circuits the router entirely — a
  configured binding can never override a test/dry-run's mock mode.
- **`VaultBinding`** (renamed from `GcpProjectBinding`) gains `backend`
  (`local`/`gcp`/`aws`) and `sync_mode` (`direct`/`cached`) fields. Config lives in the new
  `PORTUNUS_HOME/vault-bindings.json`, with a migration-safe fallback to the legacy
  `gcp-bindings.json` (old schema, defaulting to `backend="gcp", sync_mode="direct"` — the real
  vault's existing bindings keep working with zero manual migration).
- **Recency-aware, pull-only sync-down cache** (`SyncingBackend`) — a project with
  `sync_mode="cached"` fetches from GCP once, caches it in the local encrypted vault, and only
  re-fetches when the remote's value has actually changed (a cheap `gcloud secrets versions
  describe` recency check on every access). GCP → local only, never the reverse. `portunus sync
  <project>` / `portunus_sync` (MCP) force an explicit recency check ahead of relying on
  incidental access timing — useful for materializing a fresh set of secrets once at deploy time.
- **`portunus bindings set --backend/--sync-mode`**, extended `bindings show`/
  `portunus_bindings_show` output.
- **`portunus_drop_bulk`** (MCP) / **`portunus drop-bulk <file.json>`** (CLI) — create many
  local-vault secrets in one call, motivated by a real "try ~100 candidate passwords safely"
  workflow. A malformed entry is reported separately without aborting the rest of the batch.
  `portunus_drop`/`drop` also gain the `backend` override parameter their own design had
  described but hadn't wired up yet.
- **UI**: Project Explorer's read-only WIF badge becomes a real per-project backend/sync_mode
  editor (new `/api/bindings` route). DetailDrawer gains a distinct, inert "Auto-rotate…" stub
  signaling the future real-key-rotation direction, kept separate from the existing, working
  manual "Rotate…" flow.

### Fixed

- Project Explorer's `load()` no longer lets one failing endpoint (`discover`, now an expected
  failure for a local-only project with no live GCP project at all) block the other two
  (`list`/`bindings`) from rendering — found via live browser testing during this release.

## [0.12.0] - 2026-08-14

### Added

- **`portunus_drop`** — the create-side counterpart to the read/inject MCP tools, letting a
  handed-off agent instance create a new secret in the local vault end-to-end without shelling
  out to the CLI. Local-vault only (fails closed with the identical message the CLI's own
  `drop` uses if the backend is `gcloud`/`aws` — Portunus still has no write path into GCP
  Secret Manager or AWS). Lands new references at `state=dropped`, mirroring `cmd_drop` exactly.
  `value` is the one argument across the whole MCP surface that flows *in* from the caller's
  context rather than out of Portunus — inherent to being handed a secret to store, not a
  boundary violation (see README's MCP section for the full reasoning). Three layers of
  scrutiny given the risk: an AST check confirming no `Return` expression references the
  `value` name, line-by-line review, and an explicit docstring instruction telling the caller
  not to echo the value back to the human after a successful store.
- **`portunus_state`** — a thin MCP wrapper over `Registry.set_state()`, mirroring `cmd_state`.
  Closes the loop `portunus_drop` deliberately leaves open (fail-closed `state=dropped` by
  default) — promotes a freshly-created secret to `state=enabled` without a CLI round-trip.
  Pure metadata; no backend or value ever touched.
- Installed the `portunus-ask` skill at Claude Code's user scope (`~/.claude/skills/`) in
  addition to this repo's copy, so other Claude Code instances see it automatically —
  previously it was only visible inside this one repo.

## [0.11.0] - 2026-08-13

### Added

- **`portunus mcp`** — a stdio [MCP](https://modelcontextprotocol.io) server, a third OSTIARIUS
  entry point (alongside the CLI and the standalone UI) so other agents/harnesses can query and
  inject Portunus secrets directly, in-process, without ever asking a human for a key. Eight
  tools: `portunus_health`, `portunus_list`, `portunus_tree`, `portunus_ask_preview`,
  `portunus_bindings_show`, `portunus_discover`, `portunus_resolve_to_tempfile`, and
  `portunus_resolve_exec` — the last two are the injection tools, both using the same dual
  `name`/`tags` addressing as the CLI's own `inject`/`ask`, never raw `{{secret:NAME}}` syntax.
- **`portunus_resolve_exec`** — the "make the call for me" tool. Runs a caller-supplied command
  with a `{{secret}}` marker substituted through a capturing `subprocess.run` (30s timeout, not
  the CLI's default `execvp`) and returns only `{stdout, stderr, returncode}` — never the
  resolved command line, on any path including timeouts/exceptions. Verified live against the
  real Google Generative AI API using the real `personalsites-487021` Gemini key: a real API
  response came back, the key itself never appeared in the tool's result.
- **`portunus auth login <email>`** / **`portunus auth status [--json]`** — bounded auth
  lifecycle through Portunus instead of bare `gcloud`. `login` is a thin wrapper around
  `gcloud auth login` (still opens a real browser); `status` cross-references every configured
  `gcp-bindings.json` account against `gcloud auth list`'s credentialed accounts, per-binding.
  Not automatic reauth by design — a single control surface, not a reauth guarantee.
- Registered `portunus mcp` in this environment (`claude mcp add --scope user portunus`) and
  verified end-to-end via a raw JSON-RPC handshake/tools-list/tools-call script (this session
  cannot attach to a server registered mid-conversation).

## [0.10.0] - 2026-08-13

### Added

- **`group`/`related` metadata.** `Reference` gains a hierarchical `group` path (e.g.
  `project-y/supabase/auth`) and explicit `related` cross-references to other reference names
  -- additive, excluded from tag-matching, same precedent as description/purpose. `--group`/
  `--related` on `reg add`/`drop`/`retag`.
- **`portunus tree [--project X] [--json]`** -- the LLM-facing hierarchy/relationship query.
  Renders every reference's group as a real tree; a reference with no group renders under an
  `(ungrouped)` bucket rather than disappearing (the common case for freshly-discovered
  projects). `related` entries not in the current result set are marked `(unresolved)`, never
  dropped or erroring.
- **UI: DetailDrawer** shows and edits group/related through the same Move form/retag path.
- **UI: Project Explorer's Registered list now renders as a nested tree**, built client-side
  from the same data already fetched -- an independent TypeScript implementation of the same
  grouping rule as the Python CLI, verified to agree exactly against real data.
- Applied to the real vault: `personalsites-487021`'s Resend key pair grouped and
  cross-linked; **all 342 real `ffe-cicd` secrets organized into ~20 real application groups**
  (event-api/dev+prod, social-engine/dev+prod, shindig, game-library, monitoring,
  orchestration, venues, stripe, and more) by naming convention, replacing an undifferentiated
  flat list with a real navigable structure in both the CLI and the live UI.

## [0.9.0] - 2026-08-13

### Fixed

- **Multi-account GCP bindings.** Found live: authenticating a second GCP account
  (`gcloud auth login`) silently broke access to every project governed by the first, because
  no Portunus GCP code path passed an explicit `--account=` -- everything followed gcloud's
  single mutable "active account" pointer. Both credentials were never actually lost (`gcloud`
  already stores multiple accounts simultaneously); the fix makes every GCP call explicit.

### Added

- `GcpProjectBinding` gains `account: str` -- a local gcloud CLI identity to use per project,
  mutually exclusive with a WIF binding on the same project (a minted token already carries
  identity). `GcloudBackend.access()` (the real value-fetch/injection path) and
  `discover.py::list_gcp_secrets()` (the exact path that broke live) both pass `--account=`
  when configured.
- **`portunus bindings set/show`** -- previously no CLI command existed to configure
  `gcp-bindings.json` at all (only `save_gcp_bindings()`, called by tests). `set` is an
  upsert (only passed fields change); `show` prints real account/WIF-audience values (a
  local-CLI-reading-its-own-config trust boundary, deliberately different from the UI's
  presence-only `wif_configured`).
- Verified live against two real GCP accounts in one session:
  `portunus discover --project ffe-cicd` (342 secrets, `account=mdostal@ff.events`)
  immediately followed by `portunus discover --project personalsites-487021` (36 secrets,
  `account=mathew.dostal@gmail.com`), both succeeding regardless of which account gcloud
  considered "active" -- and confirmed the pre-fix failure mode by removing the binding and
  reproducing the exact `Permission denied` error the user hit.

## [0.8.0] - 2026-08-13

### Added

- **Metadata write support.** `portunus reg add`/`drop` gain `--description`/`--purpose`/
  `--injected-as`; `Registry.retag()` can now update all three in place (no collision check --
  they're not tag-matchable). `--injected-as` reuses the existing `_parse_tags()` helper.
- **UI: metadata display + edit.** Console/Vault Map/DetailDrawer show description/purpose/
  injected_as when set; DetailDrawer's existing Move form gains the same three fields,
  editing through the same `portunus retag` path -- no second write surface.
- **`portunus discover --json`**, plus a new `wif_configured` boolean field (from
  `load_gcp_bindings()` -- never the WIF audience string itself).
- **UI: Project Explorer** (new third tab) -- a GCP-project-scoped view combining what's
  already registered (`/api/list`), what's discoverable in live GCP Secret Manager
  (`/api/discover`) with a single "Register all" action (the CLI's `--register` has no
  per-secret selection, so the UI never implies one), and a GCP-WIF-configured indicator.

## [0.7.0] - 2026-08-13

### Added

- **Richer secret metadata.** `Reference` gains `description`/`purpose`/`injected_as`
  (`{env: "env:VAR"|"file:path"}`) -- additive, non-tag-matchable, round-trips through
  existing registry files unchanged.
- **Keyless GCP backend (Workload Identity Federation).** Ported the tested `auth.py`
  module (`GCPWorkloadIdentityAuth`, `AWSWebIdentityAuth`, `assert_no_long_lived_cloud_keys`)
  from this repo's own `dos-81-keyless-wif` branch. `GcloudBackend` is now multi-project
  aware -- `PORTUNUS_HOME/gcp-bindings.json` (`0600`) maps project -> WIF audience; two
  references pointing at different GCP projects each mint against their own binding in
  the same process. Access tokens are 0600-tempfile-then-unlink, never logged/printed/
  returned. `portunus auth gcp [--project]` reports identity/scope/expiry only.
- **GCP secret discovery.** `portunus discover --provider gcp --project <id> [--register]`
  -- read-only enumeration of what already exists in a live GCP Secret Manager project
  (names + labels + create-time, never a value; `discover.py` holds no reference to any
  backend's `access()` method at all). `--register` writes not-yet-registered secrets as
  `state=requested` placeholders, description seeded from GCP labels, local name derived
  as `<project>-<sm-name>` to avoid cross-project collisions, never overwrites an existing
  reference. Manually validated against two real GCP projects.
- **LLM-facing "list keys for project" query.** `Registry.list_by_project()` -- metadata
  only, zero-to-many, a sibling method to `resolve_by_tags()` rather than an overload of
  it. `portunus list --project <id>` and `portunus ask "what secrets are available for
  X"` (new `list` intent kind, alongside fetch/add/rotate).
- **AWS Secrets Manager backend stub.** `AWSSecretsManagerBackend` -- `access()` raises
  clearly, zero AWS SDK/network calls. `PORTUNUS_BACKEND=aws` selects it; fixes a real
  gap found during implementation where an unrecognized backend kind silently fell
  through to the local-encrypted default instead of failing closed.
- README's ARCA/OSTIARIUS sections now narrate the full multi-backend vision (pluggable
  stores selected per-Reference by provider+project) with a worked discovery example.

## [0.6.0] - 2026-08-13

### Added

- **L2 plugin lifecycle groundwork (scope 1 of 2).** `GET /api/health` -> `{"status":"ok"}` --
  a trivial liveness signal, never touches the CLI/subprocess. `next.config.mjs` gains
  `output: "standalone"` so `node .next/standalone/server.js` is a single fixed entrypoint the
  Pantheon host can supervise (`start: node`), matching Janus/Consus/Mnemosyne. `manifest.json`
  gains a `capabilities` list and `health_endpoint`.
  Researched directly against `mdostal/pantheon-v2`'s actual contract code. Registering these
  facts in that repo's shared manifests (`pantheon.gods.yaml`, `plugins.manifest.yaml`,
  `docs/PORTS.md`) is scope 2 -- a separate PR against that repo, for operator review.

## [0.5.1] - 2026-08-13

### Added

- UI: a `⟳ rotation requested` badge in Console, Vault Map, and DetailDrawer wherever a
  reference has `tags.rotation_requested=true` (set by an agent's `portunus ask "rotate ..."`
  request, v0.3.0) -- closes a display gap, no backend change.

## [0.5.0] - 2026-08-13

### Added

- **CLI exposure for session storage.** `portunus session store|load|inspect|list|remove`
  wires the v0.4.0 session-storage library API into the CLI. `store` mirrors `drop`'s
  stdin-only-in discipline; `load` mirrors `resolve`'s tempfile-only-out discipline exactly --
  a session record contains real cookies/tokens, exactly as sensitive as a secret value, so it
  gets a `0600` temp file and only the path is ever printed. `inspect`/`list` are metadata-only.
  `store`/`load`/`remove` write audit entries; `inspect`/`list` don't, matching the existing
  unaudited-read convention.

## [0.4.0] - 2026-08-13

### Fixed

- **`LocalEncryptedBackend.load_session()` now enforces its own TTL.** Previously an expired
  session was returned identically to a valid one -- `load_session()` never checked its own
  `ttl.expires_at` metadata. Raises `SessionExpired` (a `BackendError`) by default;
  `allow_expired=True` opts out for metadata-only callers.

### Added

- `list_sessions()` -- enumerates every stored session's metadata (namespace/TTL/rotation/
  `expired`), never a session payload. Skips corrupt/undecryptable entries rather than failing.
- `inspect_session()` gains an `expired: bool` field.
- CONTEXT.md documents session-storage vocabulary (`SESSION_SCHEMA`, `store_session`, etc.)
  that shipped before this release (PAN-7831) but was never written down.

## [0.3.0] - 2026-08-13

### Added

- **Agent-initiated add/rotate requests.** `portunus ask` now recognizes add/rotate language
  (`intent_kind`, `intent.py`) and routes to a *request*, never a fulfillment — an agent can
  ask for a secret to be added (`--name`/`--tags` required — free text can't safely name
  something brand new) or flag an existing one for rotation, without ever supplying or seeing
  a value. Add creates a `state=requested` placeholder (`Registry.request()`); rotate sets a
  metadata marker (`Registry.retag()`) on the existing reference. Fulfillment still requires a
  human running `portunus drop`.
- **Move/re-tag.** `portunus retag <name> --provider/--project/--env/--tags` updates a
  reference's tags in place, rejecting any change that would collide with a different existing
  reference. A Move action in the UI's DetailDrawer does the same via a new `/api/retag` route.
- **`requested` lifecycle state** — fails closed via `Broker.check_injectable` exactly like
  `dropped`/`revoked`. Found and fixed a related gap: `check_injectable` used a
  dropped/revoked *denylist* that would have silently treated `requested` (or any future new
  state) as injectable; flipped to an enabled/locked *allowlist* so new states fail closed by
  default.
- **`--home <path>`** — explicit, per-invocation vault override for `PORTUNUS_HOME` (cross-repo
  targeting). Not automatic multi-vault federated search, which remains out of scope.
- UI: a distinct `requested` state pill in Console/Vault Map.

## [0.2.0] - 2026-08-13

### Added

- **Metadata-tag lookup.** `Reference` gains structured `provider`/`project`/`env` fields plus
  an open `tags{}` dict, alongside the existing `scope`/`kind`. `Registry.resolve_by_tags()`
  resolves a partial tag query to exactly one reference, or fails closed with `NoMatch`/
  `AmbiguousMatch` — never a guess. Existing references migrate additively via
  `migrate_legacy_tags()`. A coarse file lock now serializes registry writes.
- **`portunus find --tags ...`** — CLI lookup by tags, metadata only, never a value.
- **Boundary injection adapters.** `portunus inject --tags ... --target env|file` injects a
  resolved secret directly into a process environment variable or a templated `.env`/JSON/YAML
  file, via a new `SecretAdapter` abstraction (`EnvVarAdapter`, `FileAdapter`,
  `HttpHeaderAdapter`, `HttpBodyAdapter`). Every injection is boundary-safe (the value is never
  returned, printed, or logged, including on the failure path) and produces an
  `adapter_resolution` audit entry.
- **Semantic front door.** `portunus ask "<plain-language request>"` maps free text to a tag
  set via deterministic matching against the registry's own known vocabulary (`intent.py`),
  then resolves and optionally injects. Fails closed at both the parsing and resolution layers
  — never guesses. Ships with a thin Claude skill (`.claude/skills/portunus-ask/`) so an agent
  can invoke it as a tool call.
- **Standalone UI (`ui/`).** A localhost-only Next.js app: Console (default tab — faceted
  table + detail/audit drawer), Vault Map (second tab — cards grouped by provider/project), and
  a persistent Ask Bar side panel. Every API route shells out to the same gated `portunus`
  console script rather than reimplementing any gating logic in TypeScript. The add-secret
  form is the sole human-plaintext-entry point, mirroring `portunus drop --stdin`.
- `portunus audit --json [--secret SM_NAME]` — machine-readable audit output for UI/tooling
  consumers.
- `portunus drop` now accepts `--provider`/`--env`/`--tags` so newly-added secrets can carry
  the full tag schema.

### Changed

- `portunus ask` without `--target` is now a resolve-only preview (success, not an error) —
  lets a caller see the match before committing to an injection target.

## [0.1.0] - 2026-07-07

Initial release: reference registry, OSTIARIUS boundary-only resolver, Petitio approval-gate
broker, ARCA backends (local-encrypted default, GCP Secret Manager, mock), tamper-evident
audit chain, and the `portunus` CLI (`reg`, `drop`, `resolve`, `gate`, `approve`, `grant`,
`state`, `status`, `audit`, `verify`). Arca session vault storage (TTL/rotation-metadata
browser session persistence) added in the same line via PAN-7831.
