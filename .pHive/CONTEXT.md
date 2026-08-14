# Project CONTEXT

Portunus is a secret broker: agents/callers reference secrets by name, and a plaintext
value is substituted only at the execution boundary — never inside an LLM/agent context.

## Terminology

- **OSTIARIUS** — the gatekeeper API: the only path to request a secret from the vault or
  deposit one into it. **Three entry points, one implementation underneath:** `resolver.py`
  (boundary substitution) + `cli.py` (the `portunus` command, the terminal), the UI's Next.js
  API routes (shell out to the CLI), and `mcp_server.py` (`portunus mcp`, an MCP stdio server so
  other agents/harnesses — not just this one — can query and inject secrets directly, in-process
  library calls since it's Python-in-the-same-package). No entry point reimplements gating —
  `Broker.check_injectable`/`Resolver` stay the single implementation everywhere.
- **ARCA** — the vault store, behind one `SecretBackend` interface. **Actually selected
  per-Reference/per-project, not one global choice** (portunus-vault-routing closed the gap
  between this claim and reality): `Resolver`'s router picks a backend via 3-level precedence —
  a reference's own `backend` override, else its project's `VaultBinding.backend`, else the
  process-wide `PORTUNUS_BACKEND` env var as the final fallback. Backends: `LocalEncryptedBackend`
  (default, local-encrypted tier), `GcloudBackend` (GCP Secret Manager, keyless via WIF,
  multi-project via `VaultBinding`), `AWSSecretsManagerBackend` (interface-conformant stub —
  `access()` raises, no real AWS calls yet). A project's `VaultBinding` may also set
  `sync_mode="cached"` — a recency-aware, pull-only sync-down cache (`SyncingBackend`, GCP → local
  only, never the reverse) instead of a live fetch every access. `MockBackend` is the in-memory
  test double; `PORTUNUS_BACKEND=mock` always short-circuits the router entirely, regardless of
  any configured binding.
- **Petitio** — the approval-gate wrapper (`broker.py`, class `Broker`). Wraps every OSTIARIUS
  request so access is always gated: grant / gate / approve + lifecycle guard.
- **Reference** — a registry entry: `name -> Secret Manager location`, plus scope, kind,
  lifecycle `state`, and approval gate. Never carries the value itself.
- **Placeholder** — a `{{secret:NAME}}` token. `Resolver` substitutes it with the live value
  only at the outbound API/tool/build call boundary, after the model has produced its output.
- **Lifecycle state** — one of `enabled`, `locked` (both injectable), `dropped`, `revoked`
  (both fail closed). See `registry.py::VALID_STATES`.
- **Boundary sink** — the only three places a resolved value may go: a caller-supplied
  callable, the argv of an exec'd subprocess, or a `0600` temp file the caller deletes. A
  resolved value is never returned up the stack and never logged.
- **Audit chain** — the tamper-evident hash-chain access log (`audit.py`) underneath every
  resolution/gate decision; `portunus verify` proves it untampered. Records ref/target/when,
  never the value.
- **Tag schema** — `Reference`'s structured `provider`/`project`/`env` fields plus an open
  `tags{}` dict, alongside the legacy `scope`/`kind` strings kept for back-compat. Populated
  via `portunus drop --provider/--project/--env/--tags` or `reg add`; migrated additively from
  legacy references by `Registry.migrate_legacy_tags()`.
- **`resolve_by_tags()`** — `Registry`'s fail-closed metadata lookup: a partial tag query
  returns exactly one `Reference` or raises `NoMatch`/`AmbiguousMatch` — never a guess. The
  foundation every other metadata-lookup path (CLI `find`, `ask`, the UI) builds on.
- **Adapter** (`SecretAdapter`, `adapters.py`) — a boundary-only injection sink beyond the
  original three (callable/temp-file/subprocess-argv): `EnvVarAdapter` (process env),
  `FileAdapter` (templated `.env`/JSON/YAML), `HttpHeaderAdapter`/`HttpBodyAdapter` (outbound
  HTTP). Every adapter's `inject()` must never return, log, or print the value, including on
  its failure path. Dispatched via `Resolver.resolve_call`'s boundary-callable sink.
- **`parse_intent()`** (`intent.py`) — the semantic front door's text-to-tags step: maps a
  natural-language request to a partial tag dict using deterministic matching against the
  registry's own known vocabulary (no NLP model). Fails closed (`AmbiguousIntent`) on anything
  unrecognized or internally conflicting; downstream ambiguity across multiple still-matching
  references is `resolve_by_tags()`'s job, not this function's. Backs `portunus ask` and the
  UI's Ask Bar.
- **`intent_kind`** — `classify_intent_kind()`'s output (`fetch` | `add` | `rotate` | `list`),
  carried on `parse_intent()`'s return value (`ParsedIntent`, a dict subclass — still unpacks
  via `**result` unchanged). Narrow, whole-word keyword classification; defaults to `fetch` (the
  safe default) on anything not recognized. More than one kind's keywords matching raises
  `AmbiguousIntent` rather than picking one.
- **`requested` state** — an agent-initiated placeholder lifecycle state (`Registry.request()`):
  a value-less `Reference` an agent asked for, fails closed via `Broker.check_injectable`
  exactly like `dropped`/`revoked`. An agent can only ever *request* an add/rotate — the actual
  value still flows exclusively through the harness-side-only `drop` path, human-originated.
- **`retag`** — `Registry.retag()` updates a reference's `provider`/`project`/`env`/`tags` in
  place, rejecting any change that would collide with a different existing reference (reuses
  `matches_tag()`'s exact-match logic — one collision definition, not two). CLI: `portunus
  retag`. UI: the Move action in `DetailDrawer`.
- **`--home`** — a per-invocation CLI override for `PORTUNUS_HOME` (explicit cross-repo vault
  targeting, not automatic multi-vault federation — that's still out of scope). Implemented as
  a save/set/restore of the env var around dispatch in `cli.py::main()`, not a threaded
  parameter, so every `Registry()`/`AuditChain()` construction site is covered automatically.
- **`description`/`purpose`/`injected_as`** — additive `Reference` metadata (default `""`/
  `""`/`{}`): what a secret is, what it's for, and `{env_name: "env:VAR" | "file:path"}`
  documenting how it's injected per environment. Descriptive only — NOT in
  `_STRUCTURED_TAG_FIELDS`, so they never participate in `resolve_by_tags()` matching.
- **`group`/`related`** — additive `Reference` metadata (default `""`/`[]`), also excluded from
  `_STRUCTURED_TAG_FIELDS`. `group` is a hierarchical path (e.g. `project-y/supabase/auth`)
  placing a secret in a tree — organizational, distinct from `project` (which IS tag-matchable
  identity, used by `resolve_by_tags`/`list_by_project`). `related` is a list of other
  reference *names* this one relates to (e.g. an auth key naming the database key it sits next
  to) — not validated against the registry at write time (forward-declaration is allowed);
  `portunus tree`/the UI mark an unresolved name rather than dropping or erroring on it.
- **`portunus tree [--project X] [--json]`** (`cmd_tree`, `_build_tree()`) — the LLM-facing
  hierarchy/relationship query. Splits each reference's `group` on `/` into a nested structure;
  a reference with no `group` renders under an `(ungrouped)` bucket at the root rather than
  being silently dropped — the common case in practice (a freshly-discovered project has zero
  grouped references until a human organizes it). Structurally cannot reach a backend/value,
  same discipline as `list_by_project()`/`discover.py`. The Project Explorer UI tab
  (`ProjectExplorer.tsx`'s `buildTree()`) is an independent TypeScript implementation of the
  same normalization rule (trim, split on `/`, drop empty segments) — no shared code with the
  Python side, but a shared, written-down contract; verified to agree byte-for-byte against
  real vault data (both the `personalsites-487021/resend` pair and all 342 `ffe-cicd`
  secrets, grouped into ~20 real apps by naming convention).
- **`list_by_project()`** — `Registry`'s metadata-only browse query (zero-to-many, no
  fail-closed single-match requirement — a sibling method to `resolve_by_tags()`, not an
  overload of it). Backs `portunus list --project` and `ask`'s `list` intent
  ("what secrets are available for X"). Structurally cannot reach a backend/value.
- **`intent_kind`** now includes `list`, alongside `fetch`/`add`/`rotate` (see below) —
  routes to `list_by_project()` via `_cmd_ask_list`, fails closed if no project is recognized.
- **`VaultBinding`** (`backend.py`, renamed from `GcpProjectBinding` in portunus-vault-routing) —
  a project id + `backend` (`local`|`gcp`|`aws`) + `sync_mode` (`direct`|`cached`) + optional WIF
  audience + optional `account`, loaded via `load_vault_bindings()` from
  `PORTUNUS_HOME/vault-bindings.json` (`0600`) — migration-safe fallback to the legacy
  `gcp-bindings.json` (old schema, defaults to `backend="gcp", sync_mode="direct"`) when the new
  file doesn't exist yet, then to `PORTUNUS_GCP_PROJECT`/`PORTUNUS_GCP_WIF_AUDIENCE` when neither
  file exists. `GcloudBackend` mints a short-lived access token per binding on `access(sm_name,
  project=...)`, written to a `0600` tempfile passed via `--access-token-file` and unlinked in a
  `finally` block — the token is a second value-class, alongside secret values themselves, that
  must never be logged, printed, or returned (see `auth.py`'s `OIDCToken`/`GCPAccessToken` —
  token fields are `repr=False`, audit entries carry only identity, never token material).
  `account` (an email string) is the practical alternative to WIF for projects with no real
  workload-identity trust configured yet: `GcloudBackend.access()` and `discover.py`'s
  `list_gcp_secrets()` pass `--account=<email>` (mutually exclusive with `--access-token-file`)
  so multiple already-locally-authenticated gcloud accounts can be used correctly per project in
  the same process, independent of gcloud's single mutable "active account" pointer — this
  fixed a real bug (portunus-gcp-multi-account): authenticating a second GCP account silently
  broke every project governed by the first, since no code path passed `--account=` explicitly.
  `portunus bindings set/show` is the CLI surface for configuring bindings (previously no
  command existed at all — only `save_gcp_bindings()`, called by tests). `show` prints real
  `account`/`wif_audience` values (a different, deliberate bar than the UI's presence-only
  `wif_configured` — a local CLI reading its own `0600` config is the same trust boundary as
  `cat`ing it directly).
- **`discover`** (`discover.py`, `portunus discover --provider gcp --project <id> [--register]`)
  — read-only enumeration of what already exists in a live GCP Secret Manager project (names +
  labels + create-time, never a value). Holds no reference to `GcloudBackend`/any
  `SecretBackend.access()` at all — structurally, not just by discipline. `--register` writes
  not-yet-registered secrets as `state=requested` (fails closed automatically); local name is
  `<project>-<sm-name>` to avoid collisions across projects that share a secret name; never
  overwrites an existing reference.
- **Session storage** (`localvault.py`, `SESSION_SCHEMA = "portunus.session.v1"`) — Arca can
  persist a Playwright-style `storageState` or other JSON-serializable session object under a
  `session:<site>:<account>` namespace (`LocalEncryptedBackend.store_session()`), with TTL and
  rotation metadata. `load_session()` fails closed with `SessionExpired` once the TTL elapses
  (`allow_expired=True` opts out for metadata-only callers). `inspect_session()`/
  `list_sessions()` return namespace/TTL/rotation/`expired` metadata only, never the session
  payload. CLI-exposed as `portunus session store|load|inspect|list|remove` — `load` mirrors `resolve`'s tempfile-only-out discipline exactly (0600 file, path only, never the record on stdout). No UI exposure yet.

## Key paths

- `src/portunus/resolver.py` — OSTIARIUS: boundary-only `{{secret:NAME}}` substitution.
- `src/portunus/broker.py` — Petitio: approval-gate wrapper around every request.
- `src/portunus/backend.py`, `localvault.py` — ARCA: `SecretBackend` implementations.
- `src/portunus/auth.py` — keyless WIF/OIDC credential minting (GCP + AWS token exchange).
- `src/portunus/discover.py` — read-only GCP secret discovery; no backend import.
- `src/portunus/registry.py` — the reference registry (metadata only, no values).
- `src/portunus/audit.py` — the hash-chain audit log.
- `src/portunus/adapters.py` — boundary injection adapters (env/file/HTTP header/HTTP body).
- `src/portunus/intent.py` — `parse_intent()`, the semantic front door's text-to-tags step.
- `src/portunus/cli.py` — the `portunus` CLI entry point (`find`, `inject`, `ask`, `drop`, ...).
- `ui/` — the standalone localhost-only UI (Console / Vault Map / Ask Bar). Every API route
  under `ui/app/api/` shells out to the same gated `portunus` console script rather than
  reimplementing any gating logic in TypeScript — see `ui/lib/portunus.ts`. Also runnable as a
  supervised service via `output: "standalone"` (`ui/next.config.mjs`) + `GET /api/health` —
  see README's "Running as a supervised service" section.
- **L2 plugin lifecycle** — the Pantheon host (`mdostal/pantheon-v2`, a separate repo) derives
  a god's L2 `ServiceDescriptor` from plain fields on its own `pantheon.gods.yaml` entry
  (`health_endpoint`/`capabilities`/`api_version`/`port`/`transport`) — no descriptor file is
  needed in this repo. Portunus's real facts (health endpoint, capabilities, port 7802) are
  registered there via a separate PR, not this repo. `manifest.json`'s `capabilities` list is
  the source those facts are drawn from.
- `.claude/skills/` — thin Claude skills wrapping the CLI/MCP surface for agent use:
  `portunus-ask` (fetch/inject by description), `portunus-drop` (create a secret, single/bulk),
  `portunus-vault-setup` (configure/check a project's backend + sync mode). Also installed at
  Claude Code's user scope (`~/.claude/skills/`) so any session on the machine sees them.
- `.pHive/epics/` — in-flight Hive epics/stories for this repo.

## Conventions

- Commit messages are ticket-prefixed (`PAN-nnnn:`, `DOS-nnnn:`), or `scaffold:`/`docs:`/`ci:`
  for non-ticket work.
- `dev -> main/master` promotion is automated (`promote.yml`); PRs merge as standard
  (non-squash) merges.
- No code path may print, return, or log a resolved secret value — see the
  `secret-boundary-invariant` cross-cutting concern (`.pHive/cross-cutting-concerns.yaml`).

## Canonical references

- `README.md` — component model, safety invariants, install/usage.
- `.pHive/project-profile.yaml` → `north_star` — Portunus's product direction: standalone
  metadata-indexed secret manager + boundary injector, UI-first, L2 Pantheon plugin second.
- `.pHive/cross-cutting-concerns.yaml` — secret-boundary and audit-chain invariants every
  story must satisfy.
