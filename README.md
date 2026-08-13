# Portunus

**A secret broker for the Dostal harness.** Named for the Roman god of keys and gates.

## Component model

**Portunus is the whole secret-broker system**, not any single piece of it. Its components carry
their own (Latin, theme-consistent) names:

| Component | Role | Where it lives today |
|---|---|---|
| **OSTIARIUS** | The gatekeeper API — the *only* way to request things from the vault or deposit things into it (the request/deposit boundary) | `resolver.py` + the `portunus` CLI (`cli.py`) |
| **ARCA** | The vault store itself — the local-encrypted tier (default, Stage 1) and the GCP Secret Manager tier (Stage 2+) behind one interface | `localvault.py` (`LocalEncryptedBackend`, default); `backend.py` (`SecretBackend`, `GcloudBackend`) |
| **Petitio** | The approval-gate wrapper — wraps every OSTIARIUS request so access is always gated (grant / gate / approve + lifecycle guard) | `broker.py` |
| *(audit)* | Tamper-evident hash-chain access log underneath all of the above | `audit.py` |

So: an agent talks to **OSTIARIUS**; **Petitio** decides whether the request may proceed; only then
does **ARCA** give up (or accept) a value — and every decision lands in the audit chain.

Portunus keeps a **reference registry** (`name -> Secret Manager location`, *never the value*) and
resolves a `{{secret:NAME}}` placeholder to a live value **only at the execution boundary** — the
actual outbound API / tool / build call, which runs *after* the model has produced its output.

> **The non-negotiable principle:** an LLM/agent context must never contain a plaintext secret.
> Secrets are referenced by **name**; the harness fetches and injects the real value at the boundary.
> The model only ever sees `{{secret:slack-bot-token}}`.

This is a **Dostal harness plugin** (`type: core`, engine `tool`) — one of the first runtime plugins
alongside the Anonymizer (PII) and Approval plugins. Multica/Hive registers its `manifest.json` and
links its Vault tab; agents call the `portunus` tool.

```
                 model sees only  {{secret:shared-anthropic}}
  ┌──────────┐   ┌───────────┐   ┌────────────────────┐   ┌──────────────┐
  │  ticket/ │──▶│  the LLM  │──▶│  PORTUNUS RESOLVE  │──▶│ outbound API │
  │  agent   │   │ (context) │   │  gate + fetch + sub│   │  (real value)│
  └──────────┘   └───────────┘   └─────────┬──────────┘   └──────────────┘
                                           │ fetch by name (boundary only)
                                           ▼
                                  GCP Secret Manager
```

## Why it's safe

- **The registry has no value field.** A `Reference` records the SM name/path, scope, kind, lifecycle
  state, and approval gate — nothing secret. It's safe to read, copy, and inspect.
- **The resolver never returns a value up the stack.** The plaintext flows only into one of three
  boundary sinks: a caller-supplied callable, an exec'd subprocess's argv, or a `0600` temp file the
  caller must delete. It is never returned to arbitrary caller/model code and never written to a log.
- **Every fetch is policy-gated.** `dropped`/`revoked` references fail closed; gated references need a
  time-boxed human approval before they resolve.
- **Every resolution is hash-chain audited** — the log records the reference/SM name and result, never
  the value; `portunus verify` proves the chain is untampered.

## Install

```bash
pipx install portunus         # once published
# or, from a clone:
pip install -e ".[test]"
```

Requires Python ≥ 3.9. **The default backend is the local-encrypted ARCA tier** (`LocalEncryptedBackend`,
`cryptography`'s Fernet recipe — AES-128-CBC + HMAC-SHA256; we never hand-roll a cipher). The master key
lives in its own `0600` file, separate from the encrypted vault file, both under `PORTUNUS_HOME` (default
`~/.portunus`, `0700`). Set `PORTUNUS_BACKEND=gcloud` (+ `PORTUNUS_GCP_PROJECT`) to use GCP Secret Manager
instead (Stage 2+); `PORTUNUS_BACKEND=mock` is for tests/dry-runs.

## Usage

### Drop a secret into Arca (harness-side only)

`drop` is how a value gets into the local-encrypted vault — from stdin or a local file, **never** an
inline flag (it would land in shell history / `ps`) and never through an LLM turn. It lands in
`state=dropped` (fail-closed) so a separate, explicit `enable` is required before it's injectable:

```bash
portunus drop shared-anthropic dostal-shared-anthropic --value-file /path/to/value   # or --stdin
portunus state shared-anthropic enabled     # now injectable
portunus state shared-anthropic locked      # optional: freeze further changes, still injectable
```

### Register a reference to an out-of-band secret (name → Secret Manager location)

Use this instead of `drop` when the value already lives in the backend (e.g. an existing GCP secret):

```bash
portunus reg add shared-anthropic dostal-shared-anthropic --scope shared --kind anthropic
portunus reg show
```

### Store browser/login session state in Arca

Arca can persist Playwright-style `storageState` or another JSON-serializable session object under a
site/account namespace. The raw vault stores one encrypted blob at `session:<site>:<account>`; inspection
returns only TTL and rotation metadata, never cookies, tokens, or local storage values:

```python
from portunus import LocalEncryptedBackend

vault = LocalEncryptedBackend()
vault.store_session(
    "example.test",
    "dostal@example.test",
    storage_state,
    ttl_seconds=3600,
    rotation_interval_seconds=900,
)
metadata = vault.inspect_session("example.test", "dostal@example.test")
record = vault.load_session("example.test", "dostal@example.test")
```

### Find, inject, and ask — metadata-tag lookup and boundary injection

References carry structured tags (`provider`/`project`/`env`, plus an open `tags{}` dict)
alongside legacy `scope`/`kind`. Lookup by tags is **fail-closed**: zero or more-than-one match
is always an explicit error, never a guess.

```bash
# Find a reference by tags -- metadata only, never a value:
portunus find --tags provider=vercel,project=mdostal.com,env=prod

# Resolve by tags and inject directly at a boundary target:
portunus inject --tags provider=vercel,project=mdostal.com,env=prod --target env --var VERCEL_TOKEN
portunus inject --tags provider=vercel,project=mdostal.com,env=prod --target file --format env --key VERCEL_TOKEN --path ./.env.local

# Semantic front door -- plain-language request instead of exact tags:
portunus ask "the vercel secret for mdostal.com in prod" --target env --var VERCEL_TOKEN
portunus ask "the vercel secret for mdostal.com in prod"   # resolve-only preview, no injection
```

`ask` never guesses either: an unrecognized or ambiguous request gets a clarifying question
on stderr instead of a pick. A thin Claude skill at `.claude/skills/portunus-ask/` wraps `ask`
for agent tool-call use.

### Resolve at the boundary

Exec mode — plaintext exists only in the child process argv, nothing is written to disk:

```bash
portunus resolve --exec curl -H "Authorization: Bearer {{secret:shared-anthropic}}" https://api.anthropic.com/v1/messages
```

Temp-file mode — writes a `0600` file and prints its **path** (never the value); caller reads + deletes:

```bash
path=$(portunus resolve "key={{secret:shared-openai}}")
# ... use "$path" ...
rm -f "$path"
```

### Policy: gate / approve / grant

```bash
portunus gate shared-anthropic          # now requires human approval to resolve
portunus approve shared-anthropic --ttl 3
portunus grant shared-anthropic serviceAccount:agent-att@proj.iam   # audited widening
portunus state shared-anthropic revoked  # emergency: blocks all injection
portunus status shared-anthropic
```

### Audit

```bash
portunus audit 25     # last 25 access decisions (names + results, never values)
portunus verify       # prove the hash chain is intact
```

## Standalone UI

A localhost-only Next.js app under `ui/` — Console (default tab, faceted table + detail
drawer), Vault Map (second tab, cards grouped by provider/project), and an Ask Bar (persistent
side panel across both, backed by `portunus ask`). No gating logic is duplicated in
TypeScript: every API route under `ui/app/api/` shells out to the same `portunus` console
script the CLI uses, so `Broker.check_injectable` stays the single, only implementation of the
gate. The add-secret form is the one deliberate human-plaintext-entry point (mirroring
`portunus drop --stdin`) — a value is piped to the CLI via stdin only, never an argv element,
never logged.

```bash
cd ui
npm install
npm run dev   # http://localhost:3000
```

## Library API

```python
from portunus import Registry, AuditChain, Broker, Resolver, GcloudBackend

registry = Registry()
broker   = Broker(registry, AuditChain())
resolver = Resolver(registry, GcloudBackend(project="my-proj"), broker)

# Inject ONLY into the boundary call; the value is never returned to us.
resolver.resolve_call(
    "Authorization: Bearer {{secret:shared-anthropic}}",
    boundary=lambda header: http_post(url, headers={"Authorization": header}),
)
```

## Development

```bash
pip install -e ".[test]"
pytest -q
```

Tests use an in-memory `MockBackend` and an isolated `PORTUNUS_HOME`, so they never touch GCP or real
state. The load-bearing test asserts that a resolved value never appears in a return value, the audit
log, or a non-`0600` file.

## Layout

```
src/portunus/
  registry.py    reference registry (name -> SM path); tags/provider/project/env; no value field
  backend.py     ARCA — SecretBackend protocol; MockBackend (tests) + GcloudBackend (Stage 2+)
  localvault.py  ARCA — LocalEncryptedBackend, the Stage 1 default (encrypted at rest)
  broker.py      Petitio — grant / gate / approve + lifecycle guard, wired to audit
  audit.py       tamper-evident hash-chain access log
  resolver.py    OSTIARIUS — boundary-only {{secret:NAME}} resolution  ← the core
  adapters.py    boundary injection adapters (env var, file, HTTP header, HTTP body)
  intent.py      semantic front door — natural language -> tag set (portunus ask)
  cli.py         OSTIARIUS — the `portunus` tool (incl. the harness-side `drop`)
ui/              standalone localhost-only UI (Console / Vault Map / Ask Bar)
.claude/skills/portunus-ask/  thin Claude skill wrapping `portunus ask`
manifest.json    Dostal plugin manifest (type: core, engine: tool)
```

## License

MIT © 2026 Mathew Dostal
