# Research Brief: portunus-standalone-core

## Requirement

Re-scope Portunus around the north star captured at kickoff (`.pHive/project-profile.yaml` →
`north_star`): a **standalone, releasable, containable secret finder/manager**, not primarily
a Dostal-harness plugin. Three capability gaps drive this epic: metadata-tag lookup, boundary
injection adapters per target, and a UI. Likely supersedes some/all of the existing
`portunus-session-vault` epic.

## Current state (verified against code)

**Layered architecture** (`src/portunus/`):
- `registry.py` — `Reference` dataclass: `name`, `sm_name`, `scope` ("shared" or a client
  slug), `kind` (freeform string, e.g. `gemini | anthropic | linear | slack`), `state`
  (`enabled|locked|dropped|revoked`, `VALID_STATES` at line 21), `approval`, `sm_path`.
  **No structured tag/metadata dict** — `scope`/`kind` are single freeform strings, not a
  provider/project/env schema.
- `resolver.py` — `Resolver` substitutes `{{secret:NAME}}` via `PLACEHOLDER_RE`. Boundary-only:
  a resolved value's only sinks are a caller-supplied callable, a `0600` temp file, or an
  exec'd subprocess argv (module docstring, lines 11-16). **No HTTP-header/body adapter, no
  env-var-file adapter** — those sinks don't exist yet.
- `broker.py` — `Broker` (Petitio): `check_injectable` gates every request against lifecycle
  state before a value is fetched (raises `NotInjectable`, `ApprovalRequired`).
- `backend.py` — `SecretBackend` protocol; `MockBackend` (in-memory), `GcloudBackend` (shells
  to `gcloud secrets versions access latest --secret=<name> [--project=<p>]`, backend.py:44-69).
  **No label/metadata read from GCP Secret Manager itself** — `GcloudBackend.access()` only
  fetches by exact `sm_name`, doesn't query SM's own resource labels.
- `localvault.py` — `LocalEncryptedBackend`, the default Stage-1 tier (Fernet AES-128-CBC +
  HMAC-SHA256), keyed separately under `PORTUNUS_HOME` (default `~/.portunus`, `0700`).
- `audit.py` — hash-chain audit log; `portunus verify` proves it untampered.
- `cli.py` — argparse CLI (283 lines): `drop`, `state`, `resolve`, and related subcommands.
  All value-producing paths either exec with the value in argv or write a `0600` temp file
  and print its path — never print the value itself.

**No UI code anywhere in this repo.** `manifest.json` declares a `ui.url` (`http://localhost:7802`)
and `ui.tab: "Vault"` with `ui.mount: "link"` — this is a **link out to something else**
(presumably the Pantheon dashboard), not a UI implemented in this repo.

**No write/rotate path for agents.** There is no "add a secret" or "roll a secret" flow an
agent can invoke semantically — `drop` is explicitly harness-side-only, stdin/file only, never
via an LLM turn (README). The north star asks for agents to request additions/rotations
*without ever seeing the value*, which is a new capability, not a UI-only gap.

## Gaps vs. the north star

1. **Metadata-tag lookup.** Need a structured tag schema (provider/project/env/scope at
   minimum) and a resolution algorithm that takes a free-form/semantic query (e.g. "the secret
   for vercel for mdostal.com") and returns exactly one match or fails closed on ambiguity.
   Today: only exact `name` lookup, plus two loose string fields.
2. **Injection adapters per target.** Need adapter implementations beyond the current three
   boundary sinks: HTTP header, HTTP body/JSON field, environment variable (process env, not
   just subprocess argv), and file (already partially covered by the temp-file sink, but not
   templated per target format e.g. `.env`, JSON, YAML).
3. **Semantic agent-facing operations.** Need a way for an agent to ask (via a skill/tool
   call, not a raw CLI flag with a value) to fetch-and-inject, add, or roll a secret, all
   without the value ever entering the agent's context.
4. **UI.** Nothing exists. Needs to show references (never values), lifecycle state, allow
   add/move/roll operations, and reflect the audit trail.
5. **L2 Pantheon plugin lifecycle.** `manifest.json` already declares the plugin shape
   (`type: core`, `engine.kind: tool`) but there's no lifecycle-event wiring — this is
   explicitly secondary per the north star and out of scope for the first pass of this epic.

## Existing in-flight work

`.pHive/epics/portunus-session-vault/` (5 stories, TDD, pre-dates this reframe) covers durable
Playwright/login session storage in Arca + an Ostiarius role-scoped gate. This is a narrower,
still-valid capability (session credentials are a secret *kind*) but was planned before the
metadata/UI/injection-adapter direction was set. Story 01 (Arca storage model) and 02 (session
API) likely compose cleanly under a generalized metadata schema; story 03 (role-scoped gate)
overlaps conceptually with the broker's existing approval gate. This epic should decide
per-story whether to keep, fold in, or supersede — not blanket-discard.

## Constraints (from cross-cutting-concerns.yaml)

- `secret-boundary-invariant` — every new adapter/sink must be provably unable to return,
  log, or print a resolved value.
- `audit-chain-integrity` — every new resolution/gate/write path must land an audit entry
  (ref/target/when, never the value), and `portunus verify` must still pass.

## Validation note

No new third-party library/SDK is implicated yet (no context7 lookup needed) — this epic is
almost entirely first-party extension of the existing Registry/Resolver/Broker/Backend/Audit
layers, plus a new UI surface (tech choice TBD in design discussion) and a new adapter
abstraction. Confidence: high (verified directly against source, not inferred).
