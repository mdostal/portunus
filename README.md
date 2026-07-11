# Portunus

**A secret broker for the Dostal harness.** Named for the Roman god of keys and gates.

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

Requires Python ≥ 3.9. The production backend shells to the `gcloud` CLI; point it at a project with
`PORTUNUS_GCP_PROJECT`. State lives under `PORTUNUS_HOME` (default `~/.portunus`, `0700`).

## Usage

### Register a reference (name → Secret Manager location)

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

## Local encrypted tier (v1 — no cloud required)

For machines without WIF + a cloud Secret Manager, Portunus ships a local
encrypted vault and a swarm-compatible `bin/secrets` CLI. Values are encrypted
at rest (never plaintext on disk) under a 256-bit master key held in the
**macOS Keychain** (`portunus-local-vault`; 0600 key file fallback on headless/non-mac hosts).
Same broker, lifecycle guard, audit chain, and resolver as the cloud tier —
`LocalVault` just implements the `SecretBackend` protocol.

```bash
# store (value via hidden prompt / stdin / --file — never argv)
bin/secrets set att linear                    # -> dostal-att-linear, encrypted at rest
bin/secrets set shared gemini --file ~/key   # kind -> GEMINI_API_KEY,GOOGLE_API_KEY

# inject at dispatch: 0600 env file, stdout is the PATH only
bin/secrets inject att --out /tmp/agent.env --ttl 1800
bin/secrets expire-check /tmp/agent.env

# inject at exec: values only in the child process environment
bin/secrets exec att -- curl -H "Authorization: Bearer $LINEAR_API_KEY" ...

# boundary resolution of placeholders (the model only ever sees the ref)
bin/secrets resolve -- curl -H "Authorization: Bearer {{secret:att-linear}}" ...

# lifecycle: drop -> enable -> lock (inject-only) -> revoke (fail closed)
bin/secrets drop att linear && bin/secrets enable att linear
bin/secrets lock att linear      # `get` now refuses; inject/exec still work
bin/secrets audit && bin/secrets verify
```

Kind → env mapping matches dostal-swarm's `bin/secrets:env_names`:
`gemini→GEMINI_API_KEY,GOOGLE_API_KEY · openai/codex→OPENAI_API_KEY ·
anthropic/claude→ANTHROPIC_API_KEY · linear→LINEAR_API_KEY ·
slack→SLACK_BOT_TOKEN · github→GH_TOKEN,GITHUB_TOKEN · else <KIND>_KEY`.

Crypto: stdlib-only AEAD from standard primitives — HMAC-SHA256-CTR keystream,
encrypt-then-MAC (`hmac.compare_digest`), per-version derived keys, AAD binding
each blob to `name:version`. Master key creation feeds the Keychain via
`security -i` stdin so the key never appears in argv/ps. Set
`PORTUNUS_BACKEND=local` to point the `portunus` CLI at the same vault.

## Pantheon mount contract

`manifest.json` carries a `mount` block (also `secrets mount`): a Vault tab can
be rendered from four values-free CLI sources — `discover --output json`
(references + metadata), `status` (lifecycle/versions), `audit --output json`
(hash-chain log), and `verify` (integrity). No mount source can return a
secret value by construction.

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
  registry.py   reference registry (name -> SM path); no value field
  backend.py    SecretBackend protocol; MockBackend (tests) + GcloudBackend (prod)
  broker.py     grant / gate / approve + lifecycle guard, wired to audit
  audit.py      tamper-evident hash-chain access log
  resolver.py   boundary-only {{secret:NAME}} resolution  ← the core
  cli.py        the `portunus` tool
manifest.json   Dostal plugin manifest (type: core, engine: tool)
```

## License

MIT © 2026 Mathew Dostal
