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

## Key paths

- `src/portunus/resolver.py` — OSTIARIUS: boundary-only `{{secret:NAME}}` substitution.
- `src/portunus/broker.py` — Petitio: approval-gate wrapper around every request.
- `src/portunus/backend.py`, `localvault.py` — ARCA: `SecretBackend` implementations.
- `src/portunus/registry.py` — the reference registry (metadata only, no values).
- `src/portunus/audit.py` — the hash-chain audit log.
- `src/portunus/cli.py` — the `portunus` CLI entry point.
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
