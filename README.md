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
  registry.py    reference registry (name -> SM path); no value field
  backend.py     ARCA — SecretBackend protocol; MockBackend (tests) + GcloudBackend (Stage 2+)
  localvault.py  ARCA — LocalEncryptedBackend, the Stage 1 default (encrypted at rest)
  broker.py      Petitio — grant / gate / approve + lifecycle guard, wired to audit
  audit.py       tamper-evident hash-chain access log
  resolver.py    OSTIARIUS — boundary-only {{secret:NAME}} resolution  ← the core
  cli.py         OSTIARIUS — the `portunus` tool (incl. the harness-side `drop`)
manifest.json    Dostal plugin manifest (type: core, engine: tool)
```

## License

MIT © 2026 Mathew Dostal
