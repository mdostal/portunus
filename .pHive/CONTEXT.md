# Project CONTEXT

Portunus is a secret broker: agents/callers reference secrets by name, and a plaintext
value is substituted only at the execution boundary — never inside an LLM/agent context.

## Terminology

- **OSTIARIUS** — the gatekeeper API: the only path to request a secret from the vault or
  deposit one into it. Lives in `resolver.py` (boundary substitution) + `cli.py` (the
  `portunus` command).
- **ARCA** — the vault store itself, behind one `SecretBackend` interface: `LocalEncryptedBackend`
  (default, local-encrypted tier) and `GcloudBackend` (GCP Secret Manager tier, Stage 2+).
  `MockBackend` is the in-memory test double.
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
- **`intent_kind`** — `classify_intent_kind()`'s output (`fetch` | `add` | `rotate`), carried
  on `parse_intent()`'s return value (`ParsedIntent`, a dict subclass — still unpacks via
  `**result` unchanged). Narrow, whole-word keyword classification; defaults to `fetch` (the
  safe default) on anything not recognized as add/rotate language.
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
- `src/portunus/registry.py` — the reference registry (metadata only, no values).
- `src/portunus/audit.py` — the hash-chain audit log.
- `src/portunus/adapters.py` — boundary injection adapters (env/file/HTTP header/HTTP body).
- `src/portunus/intent.py` — `parse_intent()`, the semantic front door's text-to-tags step.
- `src/portunus/cli.py` — the `portunus` CLI entry point (`find`, `inject`, `ask`, `drop`, ...).
- `ui/` — the standalone localhost-only UI (Console / Vault Map / Ask Bar). Every API route
  under `ui/app/api/` shells out to the same gated `portunus` console script rather than
  reimplementing any gating logic in TypeScript — see `ui/lib/portunus.ts`.
- `.claude/skills/portunus-ask/` — thin Claude skill wrapping `portunus ask` for agent use.
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
