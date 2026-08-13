# Changelog

All notable changes to Portunus are documented in this file.

## [Unreleased]

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
