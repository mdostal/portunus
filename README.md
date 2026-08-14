# Portunus

**A secret broker for the Dostal harness.** Named for the Roman god of keys and gates.

## Component model

**Portunus is the whole secret-broker system**, not any single piece of it. Its components carry
their own (Latin, theme-consistent) names:

| Component | Role | Where it lives today |
|---|---|---|
| **OSTIARIUS** | The gatekeeper API — the *only* way to request things from the vault or deposit things into it (the request/deposit boundary), including metadata-only queries like "what secrets exist for this project". **Three entry points, one implementation**: the `portunus` CLI, the standalone UI's API routes, and an MCP stdio server for other agents/harnesses | `resolver.py` + the `portunus` CLI (`cli.py`) + `mcp_server.py` (`portunus mcp`) |
| **ARCA** | The vault store — **pluggable backends behind one interface**, actually selected per-Reference/per-project (a reference's own `backend` override, else its project's `VaultBinding`, else the global fallback), not one global choice. **Real today:** local-encrypted (default), GCP Secret Manager (keyless via WIF, optionally with a recency-aware pull-only sync-down cache that survives a real network outage by serving the last-known-good cached value). **Honest stubs, not yet real:** AWS Secrets Manager, HashiCorp Vault, Infisical, Doppler, 1Password, Azure Key Vault — each fails closed with a clear error and a link to [request it](.github/ISSUE_TEMPLATE/adapter-request.yaml), never silently mis-routes. See `docs/architecture.md` for the full picture. | `localvault.py` (`LocalEncryptedBackend`, default); `backend.py` (`SecretBackend`, `GcloudBackend`, `SyncingBackend`, `VaultBinding`, and the six stub classes); `auth.py` (keyless WIF/OIDC credential minting); `discover.py` (read-only enumeration of what already exists in a live provider project) |
| **Petitio** | The approval-gate wrapper — wraps every OSTIARIUS request so access is always gated (grant / gate / approve + lifecycle guard). **`Identity` + an optional `requester` parameter on `check_injectable` exist as a deliberately inert seam** — every caller is currently allowed regardless of `requester`; real role-based enforcement (a policy store, an escalation-request flow) is designed but not yet built. Adjacent, not the same thing: **rotation provenance** (`rotation.py`) records which provider could rotate a reference and whether Portunus has a real adapter yet — config only, zero real adapters today, all stubs (Vercel is the confirmed priority target for the first real one). See `docs/architecture.md` §5. | `broker.py`, `rotation.py` |
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
`~/.portunus`, `0700`). Set `PORTUNUS_BACKEND=gcloud` to use GCP Secret Manager instead — keyless by
default via Workload Identity Federation, multi-project aware (see "GCP: multi-project + discovery"
below); `PORTUNUS_BACKEND=aws` selects the AWS Secrets Manager stub (fails clearly — not yet
implemented); `PORTUNUS_BACKEND=mock` is for tests/dry-runs.

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
record = vault.load_session("example.test", "dostal@example.test")  # raises SessionExpired past its TTL
```

Also available via the CLI — `session load` follows `resolve`'s exact boundary discipline: it
writes a `0600` temp file and prints only the **path**, never the record:

```bash
portunus session store example.test dostal@example.test --value-file storage_state.json --ttl-seconds 3600
portunus session inspect example.test dostal@example.test    # metadata only
portunus session list                                        # every stored session, metadata only
path=$(portunus session load example.test dostal@example.test)   # fails closed once expired (--allow-expired to override)
# ... use "$path" ...
rm -f "$path"
portunus session remove example.test dostal@example.test
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
on stderr instead of a pick. Three thin Claude skills under `.claude/skills/` (also installed at
Claude Code's user scope so any session on the machine sees them, not just this repo) wrap the
CLI/MCP surface for agent tool-call use: `portunus-ask` (fetch/inject by description),
`portunus-drop` (store a new secret you were just handed, single or bulk), and
`portunus-vault-setup` (configure/check a project's backend + sync mode, force a sync).

`ask` also recognizes add/rotate language ("add"/"create"/"new secret", "rotate"/"roll"/
"regenerate") and routes to a **request**, not a fulfillment — an agent never supplies or sees
a value at any point:

```bash
# Requires explicit --name/--tags -- free text can't safely name/tag something brand new:
portunus ask "add a new secret" --name gh-ci-token --tags provider=github,project=portunus,env=prod

# Flags an existing reference for rotation (metadata only, value untouched):
portunus ask "rotate the vercel secret for mdostal.com in prod"
```

### Move a reference (re-tag in place)

```bash
portunus retag vercel-mdostal --env staging
portunus retag vercel-mdostal --tags team=platform
```

Rejects any change that would collide with a different existing reference's tag combination —
fails closed, same contract as `resolve_by_tags`.

### Richer metadata — what a secret is, what it's for, how it's injected, and how it relates to others

References carry `description` (what it is), `purpose` (what it's for), `injected_as`
(`{env_name: "env:VAR" | "file:path"}`, documenting how it gets injected per environment),
`group` (a hierarchical path placing it in a tree, e.g. `project-y/supabase/auth`), and
`related` (explicit cross-references to other reference names) — all optional, all additive
to the existing tag schema:

```bash
portunus reg add stripe-prod dostal-stripe-live \
  --scope shared --kind stripe --project mdostal.com --env prod \
  --group mdostal.com/stripe --related mdostal-com-mongodb-prod
# description/purpose/injected_as/group/related are also settable via retag or the UI's edit form
```

### `portunus tree` — navigate secrets by hierarchy and relationship

The LLM-facing structure query: renders every reference's `group` as a real tree, with
`related` links shown per leaf. A reference with no `group` never disappears — it renders
under an `(ungrouped)` bucket rather than being silently dropped:

```bash
$ portunus tree --project ffe-cicd
ffe-cicd/
  clerk-webhook/
    ffe-cicd-clerk-webhook-secret-dev
    ffe-cicd-clerk-webhook-secret-prod
  event-api/
    dev/
      ffe-cicd-event-api-dev-mongo-uri
      ffe-cicd-event-api-dev-jwt-secret
      ... (48 more)
    prod/
      ffe-cicd-event-api-prod-mongo-uri
      ... (39 more)
  ... (18 more apps)
```

`--json` gives the same structure as nested JSON for programmatic/UI consumption. A `related`
entry naming a reference outside the current result set is marked `(unresolved)`, never
silently dropped. The Project Explorer UI tab renders the identical tree client-side from
the same data.

### "What secrets exist for this project?" — an LLM-facing metadata query

An agent can ask what's available without ever seeing a value — metadata only, zero-to-many,
never a fail-closed single-match requirement (it's a browse, not a resolve):

```bash
portunus list --project mdostal.com
portunus ask "what secrets are available for mdostal.com"
```

### GCP: multi-project + keyless auth (WIF) + discovery

`GcloudBackend` authenticates keyless by default — no static service-account JSON, no long-lived
AWS-style key pairs (`assert_no_long_lived_cloud_keys()` enforces this). Per-project bindings
live in `PORTUNUS_HOME/vault-bindings.json` (`0600`, project → backend/sync_mode/WIF
audience/account — see "Per-project/per-reference vault routing" below); with no bindings file,
`PORTUNUS_GCP_PROJECT`/`PORTUNUS_GCP_WIF_AUDIENCE` give today's zero-config single-project
behavior unchanged. Two references can point at two different GCP projects and each resolves
against its own binding in the same process.

```bash
portunus auth gcp --project personalsites-487021   # mint + report identity/scope/expiry only
```

**Multiple GCP accounts at once.** `gcloud` already stores multiple credentialed accounts
simultaneously (`gcloud auth login <email>` adds one without removing others) — but any command
with no explicit identity follows whichever account gcloud considers "active," a single mutable
pointer. `vault-bindings.json`'s `account` field fixes this: set it per project and every
Portunus GCP call for that project passes `--account=<email>` explicitly, regardless of
gcloud's ambient active account. (Mutually exclusive with a WIF binding on the same
project — a minted access token already carries identity.)

```bash
portunus bindings set ffe-cicd --account work@example.com
portunus bindings set personalsites-487021 --account personal@example.com
portunus bindings show                          # real values -- a local CLI reading your own 0600 config
portunus discover --provider gcp --project ffe-cicd            # uses work@example.com
portunus discover --provider gcp --project personalsites-487021  # uses personal@example.com
# both work in the same session, regardless of which account gcloud currently considers active
```

Discovery is read-only and opt-in — it enumerates what already exists in a live GCP Secret
Manager project (names + labels + create-time, **never a value** — `discover.py` holds no
reference to any backend's `access()` method at all) so you register real secrets instead of
re-creating them blind. Worked example, against a real project:

```bash
$ portunus discover --provider gcp --project personalsites-487021
not-registered  AUTH_SECRET
not-registered  SANITY_API_ADMIN_TOKEN labels={'project': 'dafshiq1', 'scope': 'admin', 'service': 'sanity', ...}
not-registered  dostal-shared-gemini labels={'app': 'dostal-swarm', 'kind': 'gemini', 'scope': 'shared'}
... (19 secrets total)

$ portunus discover --provider gcp --project personalsites-487021 --register
registered  personalsites-487021-auth_secret (state=requested)
registered  personalsites-487021-sanity_api_admin_token (state=requested)
...
```

Every `--register`ed reference lands in `state=requested` — the same fail-closed placeholder
state agent-initiated `ask "add ..."` requests use — so nothing becomes injectable until a
human reviews and promotes it. The local reference name is derived as `<project>-<sm-name>`
so two different projects that happen to share a secret name never collide.

### Per-project/per-reference vault routing + sync-down caching

A reference resolves through whichever backend its own project (or the reference itself) is
actually bound to — **not** one global `PORTUNUS_BACKEND` choice for the whole process. Three
levels of precedence: a reference's own `backend` override (set via `portunus_drop`/`reg add`/
`retag --backend {local,gcp,aws}`) wins outright; else the project's `VaultBinding.backend`
(`portunus bindings set <project> --backend ...`); else today's global `PORTUNUS_BACKEND`
env var, unchanged, as the final fallback. This means `personalsites-487021` and `ffe-cicd` can
resolve correctly in the *same process* without ever setting `PORTUNUS_BACKEND=gcloud` by hand.

```bash
portunus bindings set gig-tracker --backend local              # everything in this project stays local
portunus bindings set personalsites-487021 --backend gcp --sync-mode cached
portunus bindings show                                          # backend + sync_mode per project
```

`--sync-mode cached` opts a project into a **recency-aware, pull-only** sync-down cache — GCP →
local only, never the reverse. On each access, Portunus checks the remote's current value
version against what's cached locally (a cheap, metadata-only `gcloud secrets versions describe`
call); a match serves straight from the local encrypted cache with zero remote fetch, a mismatch
(a real rotation, or the first sync) re-fetches and re-caches. `portunus sync <project>` forces
this check explicitly — useful for a deploy that wants to materialize a fresh set of secrets once
rather than a live Secret Manager round-trip per secret per instance:

```bash
$ portunus sync personalsites-487021
  synced        personalsites-487021-resend_audience_id
  already-fresh personalsites-487021-google_generative_ai_api_key
```

Config lives in `PORTUNUS_HOME/vault-bindings.json` (0600) — the successor to the earlier
`gcp-bindings.json`, read with a migration-safe fallback: if only the legacy file exists, every
entry loads with `backend="gcp", sync_mode="direct"`, byte-for-byte today's real behavior, no
manual migration step.

Bulk-import many secrets at once — e.g. importing a batch of candidate passwords/keys before
trying each one against something via `portunus_resolve_exec`, without exposing which one worked
until you check the result:

```bash
portunus drop-bulk entries.json   # [{"name": ..., "sm_name": ..., "value": ...}, ...]
```

A malformed entry is reported under a separate `failed` list and never aborts the rest of the
batch.

### Target a different vault (`--home`)

```bash
portunus --home /path/to/other-repo/.portunus reg show
```

Explicit, per-invocation vault override — not automatic multi-vault search across repos (that's
still out of scope). Omit it and `PORTUNUS_HOME`/`DOSTAL_SECRETS_HOME`/`~/.portunus` resolve as
before.

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

## MCP server — for other agents/harnesses

`portunus mcp` starts a stdio [MCP](https://modelcontextprotocol.io) server — a third OSTIARIUS
entry point (alongside this CLI and the standalone UI) so any MCP-capable agent or harness, not
just this one, can query and inject Portunus secrets directly, without ever asking a human for a
key. Same package, same process, no subprocess boundary — the server calls `Registry`/`Resolver`/
`Broker` in-process, the same way `cli.py` does.

```bash
claude mcp add --scope user portunus -- portunus mcp
```

**Tools:**

| Tool | Returns |
|---|---|
| `portunus_health` | Liveness check |
| `portunus_list(project)` | Every reference's metadata for a project — never a value |
| `portunus_tree(project="")` | Group hierarchy + related links, same shape as `portunus tree --json` |
| `portunus_ask_preview(request)` | What a plain-language fetch request would resolve to — metadata only, previews, never injects |
| `portunus_bindings_show(project="")` | Configured per-project vault bindings (backend/sync_mode/account/WIF audience) |
| `portunus_discover(project, register=False)` | Read-only diff against a live GCP Secret Manager project; `register=True` writes not-yet-registered secrets as `state=requested` |
| `portunus_resolve_to_tempfile(name="", tags=None)` | A `0600` temp file **path** holding the resolved value — never the value itself |
| `portunus_resolve_exec(argv, name="", tags=None)` | `{stdout, stderr, returncode}` from running `argv` with a `{{secret}}` marker substituted — never the resolved command line |
| `portunus_drop(name, sm_name, value, ..., backend="")` | Create a new **local-vault-only** secret — `{name, sm_name, state}`, never the value back |
| `portunus_drop_bulk(entries)` | Create many local-vault secrets in one call — `{"created": [names], "failed": [{"name","error"}]}`, never a value |
| `portunus_state(name, state)` | Change a reference's lifecycle state — `{name, state}` |
| `portunus_sync(project)` | Force a recency check for every cached-mode reference in a project — `{"synced", "already_fresh", "failed"}`, names only |

The injection tools use the same **dual addressing** as the CLI's own `inject`/`ask`: give an
exact `name` (from a prior `portunus_list`/`portunus_tree` call) or `tags` — never raw
`{{secret:NAME}}` placeholder syntax. `portunus_resolve_exec` is the "make the call for me" tool:
write a literal `{{secret}}` marker wherever the value belongs, Portunus builds the real
placeholder and runs the command through a capturing `subprocess.run` (30s timeout) instead of
the CLI's default `execvp`. A non-zero exit is returned normally, not treated as a tool-level
error — that's the wrapped command's own semantics, not Portunus's.

`portunus_drop` is the create-side counterpart, letting a handed-off agent instance set up a new
project's secrets end-to-end without shelling out to the CLI — but **local-vault only**: it
fails closed with the same message the CLI's own `drop` uses if the active backend is
`gcloud`/`aws` (Portunus has no write path into GCP Secret Manager or AWS yet — creating a real
cloud-side secret is a separate, not-yet-built capability). It lands new references at
`state=dropped` (fail-closed, same as the CLI); `portunus_state(name, "enabled")` is the separate
explicit step that makes one injectable. `value` is the one argument, across every tool in this
server, that flows *in* from the calling agent's own context rather than out of Portunus — that's
inherent to being handed a brand-new secret to store, not a boundary violation. Portunus's
guarantee there is narrower but still absolute: the value never appears in `portunus_drop`'s own
return, is never logged, and never lands in the audit chain — but the calling agent is
responsible for not re-echoing it to the human afterward, the same way it's responsible for not
reading a `resolve_to_tempfile` path back into its own output.

**Worked example — "give them the personal Gemini key," without ever handing over the key:**

```python
portunus_resolve_exec(
    argv=["curl", "-s", "https://generativelanguage.googleapis.com/v1beta/models?key={{secret}}"],
    name="personalsites-487021-google_generative_ai_api_key",
)
# -> {"stdout": "{...real model list JSON...}", "stderr": "", "returncode": 0}
# the key itself never appears in the tool's return value, this call's own output, or any log.
```

### Auth lifecycle through Portunus

```bash
portunus auth login user@example.com   # thin wrapper around `gcloud auth login` -- the one command to remember
portunus auth status [--json]          # cross-references every vault-bindings.json account against `gcloud auth list`
```

Bounded on purpose — not automatic reauth. `login` still opens a real browser (Portunus doesn't
remove that step); `status` reports which configured bindings are currently authenticated vs.
missing, per-project, so a stale credential shows up before an injection call fails on it.
Neither command ever touches a secret value — only account emails and gcloud's own credential
metadata.

## Standalone UI

A localhost-only Next.js app under `ui/` — Console (default tab, faceted table + detail
drawer), Vault Map (second tab, cards grouped by provider/project), Project Explorer (third
tab — a GCP-project-scoped view: what's already registered, what's discoverable via
`portunus discover` with a one-click "register all", and whether that project has a WIF
binding configured), and an Ask Bar (persistent side panel across all three, backed by
`portunus ask`). No gating logic is duplicated in TypeScript: every API route under
`ui/app/api/` shells out to the same `portunus` console script the CLI uses, so
`Broker.check_injectable` stays the single, only implementation of the gate. The add-secret
form is the one deliberate human-plaintext-entry point (mirroring `portunus drop --stdin`) — a
value is piped to the CLI via stdin only, never an argv element, never logged. A secret's
description/purpose/injected_as metadata is viewable and editable from the detail drawer's
Move form — never a value, always through the same `portunus retag` path.

```bash
cd ui
npm install
npm run dev   # http://localhost:3000
```

### Running as a supervised service (L2 plugin lifecycle)

The UI also builds as a self-contained, host-supervisable service — `GET /api/health` returns
`{"status":"ok"}` (a trivial liveness signal; it never touches the CLI/subprocess), and
`next.config.mjs` sets `output: "standalone"` so a single fixed entrypoint can be supervised
directly:

```bash
cd ui
npm run build
cp -r .next/static .next/standalone/.next/static   # standalone mode doesn't do this automatically
PORT=7802 node .next/standalone/server.js
```

Runs on port **7802** (declared in `manifest.json`'s `ui.url`), matching the port Portunus is
registered under in the Pantheon host's shared manifests.

### Desktop app (macOS)

A native Tauri v2 shell wraps this same UI as a menu-bar app — no more `cd ui && npm run dev`
every time. It's additive, not a replacement: `npm run dev` stays fully valid. See
`docs/architecture.md` for what it is (and isn't).

**Install** (builds locally — see `.pHive/epics/portunus-desktop-app/` for design details, or
download the `.zip` from the latest [GitHub release](../../releases) once one has shipped):

```bash
cd ui
npm install && npm run build
cargo install tauri-cli --version "^2" --locked   # once
cargo tauri build
cp -r src-tauri/target/release/bundle/macos/Portunus.app /Applications/
open /Applications/Portunus.app
```

**One-time Gatekeeper bypass.** This is ad-hoc signed, not notarized — a deliberate v1 scope
decision (one user, one machine, not a public-distribution problem; see the design doc for the
full reasoning), not an oversight. macOS will refuse to open it the first time. Go to
**System Settings → Privacy & Security**, scroll to the bottom, and click **Open Anyway** next
to the Portunus warning, then launch it again. This only happens once — self-updates afterward
don't re-trigger it.

**Using it.** A tray icon appears in the menu bar: *Open Vault*, *Check for Updates…*, *Launch
at Login*, *Quit*. Closing the window hides it (the app keeps running in the tray) — use
*Quit* to actually stop it. Updates are checked automatically every 6 hours (and on demand via
the tray item) against this repo's own GitHub releases, using your own already-authenticated
`gh` CLI — never a token embedded in the app. You'll always get a confirmation dialog before
anything installs; nothing swaps itself in silently.

**If something goes sideways** (a crashed/force-quit app can leave its background server
running): `pkill -f "server.js.*portunus"` clears it out; the next launch picks a fresh port
regardless.

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
  registry.py    reference registry (name -> SM path); tags/provider/project/env; description/
                 purpose/injected_as metadata; list_by_project() browse query; no value field
  backend.py     ARCA — SecretBackend protocol; MockBackend (tests); GcloudBackend (keyless WIF,
                 multi-project via VaultBinding); SyncingBackend (recency-aware sync-down cache,
                 falls back to the last cached value on a real connectivity failure);
                 AWSSecretsManagerBackend/VaultServerBackend/InfisicalBackend/DopplerBackend/
                 OnePasswordConnectBackend/AzureKeyVaultBackend (honest stubs, see docs/architecture.md)
  localvault.py  ARCA — LocalEncryptedBackend, the Stage 1 default (encrypted at rest)
  auth.py        keyless WIF/OIDC credential minting (GCP + AWS token exchange); never logs/
                 returns/prints minted credentials
  discover.py    ARCA discovery — read-only enumeration of a live provider project's secrets
                 (names/labels only); structurally cannot reach a value (no backend import)
  broker.py      Petitio — grant / gate / approve + lifecycle guard, wired to audit; Identity +
                 an inert requester param on check_injectable (real enforcement not yet built)
  audit.py       tamper-evident hash-chain access log
  resolver.py    OSTIARIUS — boundary-only {{secret:NAME}} resolution  ← the core
  adapters.py    boundary injection adapters (env var, file, HTTP header, HTTP body)
  intent.py      semantic front door — natural language -> tag set + intent_kind
                 (fetch/add/rotate/list) (portunus ask)
  cli.py         OSTIARIUS — the `portunus` tool (incl. the harness-side `drop`)
docs/architecture.md  adopter-facing reference: component diagram, ARCA backend-selection
                 precedence, Petitio today-vs-tomorrow, the request/resolve sequence
ui/              standalone localhost-only UI (Console / Vault Map / Ask Bar)
.claude/skills/       thin Claude skills wrapping the CLI/MCP surface: portunus-ask (fetch),
                 portunus-drop (create, single/bulk), portunus-vault-setup (bindings/sync)
.github/ISSUE_TEMPLATE/adapter-request.yaml  request a new ARCA backend
manifest.json    Dostal plugin manifest (type: core, engine: tool)
```

## License

MIT © 2026 Mathew Dostal
