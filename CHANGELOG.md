# Changelog

All notable changes to Portunus are documented in this file.

## [Unreleased]

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
