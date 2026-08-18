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
- **ARCA** — the vault store, behind one `SecretBackend` interface (a one-method Protocol —
  `access()` only; `store()`/`latest_version()` are duck-typed extras some concrete classes
  happen to implement, not part of the Protocol itself). **Actually selected
  per-Reference/per-project, not one global choice** (portunus-vault-routing closed the gap
  between this claim and reality): `Resolver`'s router picks a backend via 3-level precedence —
  a reference's own `backend` override, else its project's `VaultBinding.backend`, else the
  process-wide `PORTUNUS_BACKEND` env var as the final fallback. **Real backends:**
  `LocalEncryptedBackend` (default, local-encrypted tier), `GcloudBackend` (GCP Secret Manager,
  keyless via WIF, multi-project via `VaultBinding`). **Honest stubs** (portunus-swappable-trio
  — each `access()` unconditionally raises `BackendError` pointing to
  `.github/ISSUE_TEMPLATE/adapter-request.yaml`, no real calls, no `store()`/`latest_version()`):
  `AWSSecretsManagerBackend`, `VaultServerBackend` (HashiCorp Vault/OpenBao), `InfisicalBackend`,
  `DopplerBackend`, `OnePasswordConnectBackend`, `AzureKeyVaultBackend` — researched but deferred
  until a real validated environment exists for one, same reasoning that made GCP real in the
  first place. A project's `VaultBinding` may also set `sync_mode="cached"` — a recency-aware,
  pull-only sync-down cache (`SyncingBackend`, GCP → local only, never the reverse) instead of a
  live fetch every access; on a real connectivity failure it falls back to the last-known-good
  local copy (`last_sync_result="stale-offline"`) rather than hard-failing, without ever marking
  that copy as verified-fresh. `MockBackend` is the in-memory test double;
  `PORTUNUS_BACKEND=mock` always short-circuits the router entirely, regardless of any
  configured binding.
- **Petitio** — the approval-gate wrapper (`broker.py`, class `Broker`). Wraps every OSTIARIUS
  request so access is always gated: grant / gate / approve + lifecycle guard.
  `check_injectable(name, requester: Optional[Identity] = None)` carries a deliberately inert
  seam (portunus-swappable-trio) — `Identity` (name + kind: human/agent/system, resolved the
  same way `AuditChain`'s actor already is) is threaded through but never consulted; every
  caller is currently allowed regardless of `requester`. The `PolicyStore` half of that design
  now exists as `roles.py` (portunus-vault-trust-and-access) — `PolicyRecord(scope_type: org|
  project|env, scope_value, role, actions[])`, persisted for real in `PORTUNUS_HOME/roles.json`,
  editable via `portunus roles set/delete/show` and the UI's Settings page — but STILL not
  consumed anywhere: `check_injectable()`/`retag()` are byte-identical whether or not
  `roles.json` has content (`tests/test_roles.py`'s own defining test proves this directly, not
  just asserts it). Real enforcement (still needs an `EscalationRequest`-style evaluation
  function — most-specific-scope-wins? explicit deny beats allow? genuinely open) is future
  work — no rush, per explicit product direction.
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
- **`org`** (`registry.py`, portunus-vault-trust-and-access) — an organizational umbrella one
  level above `project` (e.g. `firefly-events` spanning `ffe-cicd`/`shindig`), IS in
  `_STRUCTURED_TAG_FIELDS` (tag-matchable, participates in `retag()`'s collision check
  alongside `provider`/`project`/`env`/`repo`) — unlike `group`, which stays free-text/
  organizational-only and unrelated to identity. Vault Map's org → project drill-down is built
  entirely on this field (no new store) — the fix for a flat card wall becoming unmanageable
  past ~30 repos.
- **Custom views** (`views.py`, `PORTUNUS_HOME/views.json`) — named, human-curated lists of
  reference *names* (not a saved query) for task-shaped clustering that doesn't map onto org/
  project/env ("everything for a specific deploy"). Deliberately orthogonal to the structural
  hierarchy. Every mutator (`create_view`/`add_to_view`/`remove_from_view`/`delete_view`) wraps
  its own load→mutate→save in one `flock` acquisition — locked from day one, unlike
  `vault-bindings.json`'s own still-unfixed retrofitted save-only lock. CLI: `portunus views
  create/add/remove/delete/show`. UI: Console's "My views" panel + per-reference add/remove in
  `DetailDrawer`.
- **`suggested` / `portunus_suggest_metadata`** (`registry.py`'s `Reference.suggested` sidecar,
  `Registry.suggest_metadata()`/`clear_suggestion()`, the MCP tool of the same name) — an agent
  proposes `description`/`purpose`/`tags`/`group` (`SUGGESTIBLE_FIELDS` — routing fields are
  structurally rejected, never suggestible: they carry resolution-time consequences an agent
  shouldn't redirect); the proposal lands ONLY in the sidecar, never the live field.
  `portunus metadata confirm <name> <field>` applies it via the SAME `retag()` a manual edit
  would use (no second write path), then clears the sidecar entry; `reject` just clears it,
  never touching the live field. UI: `DetailDrawer` shows a "suggested by \<agent\>: '...'
  [Confirm] [Reject]" block per pending field.
- **`portunus vault status` / first-run detection** — reports whether `PORTUNUS_HOME` has EVER
  been initialized (absence of BOTH
  `registry.json` and `vault-bindings.json`), checked in Python against `paths.home()`'s own
  resolution rather than duplicated as filesystem logic in TypeScript. Drives the Standalone
  UI's first-run `SetupWizard` — an already-used vault, however empty it looks, never sees it
  again.
- **`crawl_candidates()` / `portunus crawl`** (`crawl.py`, `portunus_crawl_candidates` MCP
  tool) — a discovery bundler, NOT a writer and NOT an LLM caller. For every reference missing
  description/purpose/org, bundles everything already known (`sm_name`, `group`, `project`,
  `org`, `repo`, `source_files`, its project's `VaultBinding`, its provider's
  `RotationBinding`) into one JSON object, for an external LLM/agent/human to read and act on
  via the already-shipped `portunus_suggest_metadata`. Portunus has zero LLM-API-key
  infrastructure anywhere — this bundles context rather than inventing that. Real vault data
  (393 references, checked during planning) showed `repo` filled on <1% of references —
  `sm_name`/`group` are the strongest signals actually available today; real repo-cloning stays
  deferred until that fill rate rises.
- **`generate_report()` / `portunus report`** (`crawl.py`) — renders current vault state as
  Markdown (org → project structure, each reference's known metadata, an explicit `## Gaps`
  section). Independent of `crawl_candidates()` — a real "deploy docs" starting point whether
  or not any crawl-sourced metadata has ever been confirmed.
- **`leakscan.py` / `portunus leak-scan` / leak detection** — detects whether a managed
  secret's actual decrypted value shows up somewhere it shouldn't (logs, `.claude`
  conversation transcripts, shell history, or any human-configured local path). The strictest
  secret-boundary-invariant instance in the codebase: it MUST call `Backend.access()` to get
  values to search FOR, then guarantees those values never escape beyond an in-memory per-line
  comparison — `Finding(ref_name, path, line_number, byte_offset)` structurally cannot hold a
  value. Line-based, incremental (per-file `Watermark`: byte offset + `(size, mtime)`
  fingerprint + consumed line count) — real scale data (3.4 GB / 4,421 files under one
  `~/.claude`) ruled out naive full-rescan-every-run. Three separate locked JSON stores
  (`leak-scan-config.json`, `leak-status.json`, `leak-scan-watermarks.json`), deliberately not
  one, split by write frequency. Severity (`warn`/`urgent`/`critical`) is DERIVED at read time
  from elapsed days since a reference's earliest finding (0–2/3–6/7+), never stored
  redundantly. Advisory only — proven, not asserted, that `check_injectable()`/`resolve()`
  behave byte-identically regardless of leak status, mirroring the `roles.json` stub precedent.
  `portunus leak mark-rotated <name>` is a documented human assertion Portunus can't verify;
  it also invalidates the watermark for every file where that reference had a finding, so a
  premature mark-rotated still gets caught by the next scan (a real gap the epic's own
  live-proof pass caught and fixed before shipping). MCP surface has full CLI parity
  (`portunus_run_leak_scan`, config tools, `portunus_leak_mark_rotated`) by explicit user
  decision reversing the epic's own initial "status-query only" boundary — recorded in
  `.pHive/epics/portunus-leak-scan/docs/design-discussion.md` §2's addendum, not silently
  overwritten; widening WHO can trigger a scan never widened WHAT can be scanned (still only
  human-configured paths).
- **Container deployment / `Dockerfile`** (`portunus-container-image`) — packages the CLI + MCP
  server only (not the UI) as a non-root, `PORTUNUS_HOME`-volume-declared image. Deliberately
  targets same-pod/same-host reachability (`docker exec`/`kubectl exec`, a shared pod volume, or
  Portunus starting the consumer via `resolve --exec`) rather than a network-reachable shared
  broker service — the MCP server is stdio-only today, and a genuinely shared service would need
  the currently-stub-only RBAC (`roles.py`) actually enforced, explicitly deferred future work.
  `PORTUNUS_HOME` must be a real persistent volume for the local-encrypted backend specifically
  (self-bootstrapping master key means an unmounted/removed volume silently and permanently loses
  every secret); GCP-backend-only usage is unaffected. GKE Workload Identity is the recommended
  production auth path (already keyless). One image ships with the `gcloud` CLI included rather
  than a slim/full split — a deliberate v1 tradeoff, not an oversight.
- **`LeakBadge` / leak visibility across the UI** (`portunus-leak-visibility`) — an independent
  signal from `RotationBadge`/`tags.rotation_requested` (design decision: reusing that tag would
  mean leak-scan calling `retag()` on every new finding, a write path the engine was never
  designed for, and would collapse "agent asked to rotate" and "leak detected" into one
  indistinguishable boolean). Driven by a `ref_name -> LeakSummary` map fetched once per page
  load, rendered next to `RotationBadge`/`CompletenessBadge` wherever a reference's name already
  appears (Console + a "Leaked" facet, Vault Map, Project Explorer, DetailDrawer's full
  expandable history + Mark rotated action). `summarize(..., detail=True)` (leakscan.py) is the
  backend extension this all sits on — adds `findings` (raw path/line list) and `distinct_files`
  (unique file count, the "leaked in N conversations" headline number — NOT raw finding count,
  since one transcript can match the same secret on many lines) to the existing aggregate-only
  shape, `detail` defaulting to False so no existing caller's output changes.
- **`renderReportMarkdown.tsx`** — a small custom Markdown-to-React renderer for
  `generate_report()`'s exact, narrow output shape, not a markdown-parsing dependency
  (`ui/package.json` stays at 3 runtime dependencies). Settings' "View report" button renders
  the report in-app; "Download report" still saves it as a file.
- **Git-repo scan targets / source classification** (`portunus-leak-scan-git-awareness`) —
  `portunus leak-scan config add-repo <path>` scans a repo's FULL git history (`git log --all -p
  --full-history --reverse`, dumped to a temp file per run, fed through the SAME scan_paths()
  engine unchanged, temp file always deleted). `--reverse` (oldest-first) is deliberate: new
  commits only ever append to the dump, keeping the `(path, line_number)` dedup key stable
  across an actively-developed repo's scans. Always a full re-scan, never incremental (git
  history can be rewritten). Every `Finding`/`LeakFinding` carries `source_kind` (`log` / `local`
  / `git-history` — a soft, documented heuristic for the first two) and, for git-history
  findings, `repo_path` + `repo_visibility` (`public`/`private`/`unknown`, resolved via `gh repo
  view <remote>` ONCE per repo per scan — mirrors `ui/src-tauri/src/updater.rs`'s own gh-CLI,
  user's-own-credential posture, never an embedded token; never a guess when unresolvable). A
  public-repo finding gets the loudest UI treatment anywhere it renders.

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
- `src/portunus/views.py` — custom views (`PORTUNUS_HOME/views.json`), locked from day one.
- `src/portunus/roles.py` — STUB role/policy schema (`PORTUNUS_HOME/roles.json`) — persists for
  real, consumed by nothing.
- `src/portunus/agent_setup.py` — `portunus agent init`/`status`: MCP registration + usage-skill
  install for whatever agent CLIs are on the machine. Zero secret-boundary surface by
  construction (no `Registry`/`Broker`/`Resolver` import) — local agent-CLI config plumbing only.
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
  `portunus-vault-setup` (configure/check a project's backend + sync mode), `portunus-vault-audit`
  (crawl/report/leak-scan). The canonical, packaged copy lives at `src/portunus/agent_skills/`
  (real package data, ships in the installed wheel) — `portunus agent init` installs it to
  Claude Code's user scope (`~/.claude/skills/`) on any machine, not just this repo.
  `src/portunus/agent_setup.py` also registers the MCP server for any detected agent CLI
  (Claude Code, Codex CLI today). See `docs/architecture.md` §15.
- `.pHive/epics/` — in-flight Hive epics/stories for this repo.
- `docs/architecture.md` — adopter-facing reference (component diagram, ARCA backend-selection
  precedence, Petitio today-vs-tomorrow, request/resolve sequence) — distinct from `.pHive/`,
  which is planning history, not reader-facing documentation.

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
