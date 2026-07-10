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

Portunus supports keyless cloud auth. The harness supplies a short-lived OIDC token through
`PORTUNUS_OIDC_TOKEN_FILE` (preferred) or `PORTUNUS_OIDC_TOKEN`, plus non-secret metadata:
`PORTUNUS_OIDC_ISSUER`, `PORTUNUS_OIDC_SUBJECT`, `PORTUNUS_OIDC_AUDIENCE`, and optionally
`PORTUNUS_OIDC_EXPIRES_AT`. Static GCP service-account JSON and AWS access-key pairs are rejected by
the conformance helper; they should not exist in the repo, agent env, or broker config.

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

### Keyless WIF/OIDC

GCP uses Workload Identity Federation. Configure a pool/provider outside Portunus, scoped to the
harness OIDC issuer and claims, then set:

```bash
export PORTUNUS_GCP_PROJECT=my-project
export PORTUNUS_GCP_WIF_AUDIENCE='//iam.googleapis.com/projects/123/locations/global/workloadIdentityPools/agents/providers/portunus'
export PORTUNUS_OIDC_TOKEN_FILE=/run/portunus/agent-oidc.jwt
portunus auth gcp     # mints a short-lived access token, prints only identity/scope metadata
```

When `PORTUNUS_GCP_WIF_AUDIENCE` is present, the GCP backend exchanges the OIDC token for a
short-lived access token, passes it to `gcloud` via a temporary `0600` access-token file, and deletes
that file before returning.

AWS uses `AssumeRoleWithWebIdentity`; configure an IAM OIDC provider and a trust policy scoped by the
same issuer/claim model, then set:

```bash
export PORTUNUS_AWS_ROLE_ARN=arn:aws:iam::123456789012:role/portunus-agent
export PORTUNUS_OIDC_TOKEN_FILE=/run/portunus/agent-oidc.jwt
portunus auth aws     # validates exchange without printing STS credentials
```

The trust model is intentionally external and human-reviewed: WIF pool/provider definitions, IAM role
trust policies, and claim-to-identity mappings are the security boundary. Portunus records only
identity/scope references and exchange outcomes in the audit chain; OIDC tokens, access tokens, and
STS credentials are never written to the registry, audit log, README examples, or command output.

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
  auth.py        OIDC -> short-lived GCP/AWS credentials; no static keys
  backend.py    SecretBackend protocol; MockBackend (tests) + GcloudBackend (prod)
  broker.py     grant / gate / approve + lifecycle guard, wired to audit
  audit.py      tamper-evident hash-chain access log
  resolver.py   boundary-only {{secret:NAME}} resolution  ← the core
  cli.py        the `portunus` tool
manifest.json   Dostal plugin manifest (type: core, engine: tool)
```

## License

MIT © 2026 Mathew Dostal
