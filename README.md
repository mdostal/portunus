# Portunus

<!-- shared:tagline -->
> A secret broker that finds secrets by metadata and injects them — without them ever touching your LLM. Free & open source.
<!-- /shared:tagline -->
<!-- shared:byline -->
Built by [Mathew Dostal](https://mdostal.com) — fractional CTO, Dostal Technology.
<!-- /shared:byline -->

> Choose your vault. Give your AI agents safe tooling to use it — without secrets ever touching
> the LLM.

**portunus** is a thin broker between your AI agents and the secret vault you already run. It
doesn't store your secrets or reinvent vault security — it resolves them by metadata and injects
them at the boundary, so an agent can **use** a secret (call an API, deploy, sign) without the
raw value ever landing in the model's context, logs, or tool output.

*(Portunus — the Roman god of keys and doors. Fitting.)*

**[portunus site →](https://mdostal.github.io/portunus/)** — landing page, install snippets, and
the component-model pitch in one place.

## The problem

AI agents need real secrets to do real work — deploy tokens, API keys, DB creds. But the moment a
secret touches an LLM's context, a log line, or a tool result, it's leaked. Most teams already
have a vault. What's missing is the layer that lets an **agent** use that vault safely.

That's portunus.

## What works today

- **The secret value never enters the LLM's context, logs, or tool output.** Enforced by code —
  every boundary path (the resolver's three sinks, every MCP tool, every adapter's failure path)
  is covered by a test that asserts the value never appears in a return value, stdout/stderr, or
  the audit log, including on the failure path, not just the happy one.
- **Resolve by metadata.** Ask for `anthropic / demo / prod` by what it *is*, not by memorizing a
  key name. **Fail-closed** — an ambiguous match refuses and lists candidates, never returns the
  wrong secret.
- **Inject at the boundary** — env var, file (env/json/yaml), subprocess argv, or a `0600`
  tempfile. The agent gets a *reference* or a *path*, never the value.
- **Bring your own vault** — **GCP Secret Manager** (keyless, via Workload Identity Federation)
  and a **local-encrypted vault** (Fernet: AES-128-CBC + HMAC-SHA256, zero cloud setup) work
  today.
- **One core, three doors** — an **MCP server** (13 tools, metadata-only by default), a
  **35-command CLI**, and a **Next.js UI** (plus a native macOS desktop shell around the UI).
- **Tamper-evident, hash-chained audit log** — keyed by secret *name*, never value. `portunus
  verify` proves the chain.

## Roadmap (help wanted)

- [ ] More vault backends — HashiCorp Vault, AWS, Azure, 1Password, Doppler, Infisical *(fail-closed
      stubs today — [request the one you need](.github/ISSUE_TEMPLATE/adapter-request.yaml) and it
      jumps the queue)*
- [ ] Native HTTP-client injection adapter *(today it's `portunus resolve --exec curl ...`, or a
      caller-supplied boundary callable via the library's `resolve_call` — works, but there's no
      built-in `HttpHeaderAdapter`/`HttpBodyAdapter` class yet, only `EnvVarAdapter`/`FileAdapter`)*
- [ ] Secret rotation *(the provenance layer is real — `RotationBinding`, three stub adapters — but
      every adapter still unconditionally raises; nothing rotates yet)*
- [ ] Role-based / per-agent access control *(the seam exists — `Identity` + a `requester` param on
      the one gate check — every caller is currently allowed regardless of who's asking)*

## Honest scope

Portunus guarantees that **its own** paths never leak the value. It can't stop a command *you*
wrap from echoing its own argument — `echo {{secret}}` will print the secret, because that's the
command's doing, not portunus's. That's the correct, defensible boundary, and it's documented in
the code.

## Built in the open

Solo-built and moving fast. The core is real and tested; the backends and rotation are where the
growth is. Break it, tell me where it leaks, open issues, send PRs — poking a hole in the security
model is a *gift*, open it as an issue.

## Quick start

```bash
pip install -e ".[test]"   # or: pipx install portunus, once published

# store a secret -- stdin/file only, never an inline flag (would land in shell history)
echo -n "sk-ant-..." | portunus drop shared-anthropic dostal-shared-anthropic --stdin
portunus state shared-anthropic enabled     # lands dropped/fail-closed by default; this makes it injectable

# resolve it only at the boundary -- the value never touches your shell history, a log, or an LLM turn
portunus resolve --exec curl -H "Authorization: Bearer {{secret:shared-anthropic}}" \
  https://api.anthropic.com/v1/messages
```

See [Install](#install) and [Usage](#usage) below for the full picture — registering an existing
secret instead of dropping a new one, tag-based lookup, the MCP server, and the UI.

## Architecture, vision & design decisions

- **[docs/architecture.md](docs/architecture.md)** — the adopter-facing reference: a component
  diagram, ARCA's backend-selection precedence as a decision tree, Petitio today-vs-the-designed-
  future, the full request/resolve sequence, and the rotation-provenance design — five diagrams,
  kept honest about what's real versus what's a stub.
- **Long-term direction** (from this project's own planning doc,
  [`.pHive/project-profile.yaml`](.pHive/project-profile.yaml)):
  > Portunus is a standalone, releasable, containable secret finder/manager — not just a
  > Dostal-harness plugin. [...] GCP Secret Manager is the store; Portunus is the
  > resolver+injector layer on top — that split is decided, not open. Long-term it should also
  > self-declare as an L2 Pantheon plugin [...] while still standing up its own harness
  > integration (skill/CLI) and UI when run standalone.
- **Every design decision, with the reasoning, not just the outcome** —
  [`.pHive/epics/`](.pHive/epics/) holds a research brief + design discussion (often with an
  adversarial "grill" pass) for every feature this project has shipped: why local+GCP are the
  only real ARCA backends today, why rotation ships as provenance-only, why the desktop app
  shells out to `gh` instead of embedding a token, and more. This is the actual paper trail, not
  a curated highlight reel.
- **[CHANGELOG.md](CHANGELOG.md)** — what shipped, release by release.
- **Adapter pattern** — a new ARCA backend implements one method
  (`access(sm_name, project="") -> str`, see `SecretBackend` in `backend.py`) and fails closed
  with a clear error until it's real; [request one](.github/ISSUE_TEMPLATE/adapter-request.yaml)
  or see an existing stub (e.g. `InfisicalBackend`) as a template.

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

### Running in a container

For most real deployments this is the actual install path — not "download the desktop app,"
but "pull an image and run it alongside whatever you're already deploying." The `Dockerfile` at
the repo root builds the CLI + MCP server (not the UI — see `docs/architecture.md` for why that's
a deliberate v1 scope line); it works unchanged under Docker or Podman.

```bash
docker build -t portunus .
docker run --rm -v portunus-home:/home/portunus/.portunus portunus resolve --exec curl \
  -H "Authorization: Bearer {{secret:shared-anthropic}}" https://api.anthropic.com/v1/messages
```

**⚠️ `PORTUNUS_HOME` must be a real, persistent volume for the local-encrypted backend.** The
image declares it as a `VOLUME`, so an unmounted run still gets a persistent anonymous volume —
but if that volume is ever removed (`docker rm -v`, `docker volume rm`, an ephemeral CI runner
that never mounts one at all), `master.key` regenerates from scratch on next start and every
previously-stored secret becomes **permanently unrecoverable**. Always bind-mount or name a real
volume in anything beyond a throwaway test. This risk doesn't apply if you're using the GCP
backend only (`PORTUNUS_BACKEND=gcloud`) — there's no local ciphertext to lose.

**Two real deployment shapes**, not one abstract "containerize it":

- **CI/build-step**: exactly the command above — a one-shot container that resolves a secret
  into a single command's argv, same boundary-only guarantee as running the CLI locally.
- **Kubernetes sidecar**: Portunus's container and your app's container share one pod, so they
  already share a network namespace and can share a volume for free. The app reaches Portunus
  via `kubectl exec` (or Portunus starts the app itself: `resolve --exec <app-start-command>`,
  the same pattern the CI example uses) — never a network call, never a second trust boundary.
  This is a deliberate v1 scoping decision, not an oversight: the MCP server is stdio-only today,
  which makes same-pod/same-host reachability the natural fit; a network-reachable *shared*
  Portunus service many pods call would need the currently-stub-only RBAC (`roles.py`) to
  actually be enforced — real, larger, explicitly future work.

**Auth per backend, per environment:**

| Environment | Local-encrypted backend | GCP backend |
|---|---|---|
| Local Docker/Podman dev | zero-config — self-bootstraps on first write | mount your host's `~/.config/gcloud` read-only (`-v ~/.config/gcloud:/home/portunus/.config/gcloud:ro`) so the container reuses your own already-authenticated `gcloud` identity |
| Real Kubernetes | mount a real PersistentVolume | **GKE Workload Identity** (a k8s ServiceAccount bound to a GCP service account) — keyless, no credential ever touches the container image or a volume. This is the recommended production path, not one option among several. |

**`docker-compose.yml`** at the repo root is a runnable worked example of the sidecar pattern —
`docker compose build && docker compose run --rm portunus drop ...` — see the file's own header
comment for the full walkthrough.

**Podman note.** Podman's rootless-by-default model remaps container UIDs to an unprivileged
host UID range, which can affect bind-mounted (not named-volume) host directory permissions
differently than Docker. If a bind mount isn't writable as expected, try
`podman run --userns=keep-id ...`. Named volumes (as in the examples above) sidestep this
entirely — prefer them over host bind-mounts unless you specifically need host-path access.

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
  --scope shared --kind stripe --org firefly-events --project mdostal.com --env prod \
  --group mdostal.com/stripe --related mdostal-com-mongodb-prod
# description/purpose/injected_as/group/related are also settable via retag or the UI's edit form
```

`org` sits one level above `project` — an organizational umbrella spanning several projects
(e.g. `firefly-events` spanning `ffe-cicd`, `shindig`, and more), the structural field the
Standalone UI's Vault Map drill-down and Console's facets key off of. Like every other
structured tag, an absent `org` is never an error — it lands in a `(no org set)` bucket, same
non-dropping treatment `group`'s own `(ungrouped)` bucket already gets.

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

### `repo`/`source_files` — which git repo (and file) actually consumes a secret

A real gap, found by looking at the real data: `group` already captures a rough service/env
hierarchy (`ffe-cicd/event-api/prod`), but nothing distinguished *which git repo* consumes a
secret from *which cloud project* it lives in — one shared GCP project (`ffe-cicd`) can span
many repos/services. `repo` is a new structured field (`find --tags repo=event-api` works
immediately, same as `provider`/`project`/`env`); `source_files` is a list of file paths in that
repo that reference the secret (a `docker-compose.yml`, a CI workflow) — same optional,
human-filled posture as `related`, not auto-discovered:

```bash
portunus reg add stripe-prod dostal-stripe-live --repo billing-service
portunus retag stripe-prod --source-files docker-compose.prod.yml,.github/workflows/deploy.yml
portunus find --tags repo=billing-service
```

**Backfilling many references at once** — the real unlock for hundreds of already-grouped
secrets, not a one-at-a-time `retag` per reference:

```bash
# Preview first -- makes zero writes, reports exactly what would change:
portunus retag-bulk --group-prefix ffe-cicd/event-api --repo event-api --dry-run

# Then actually apply it:
portunus retag-bulk --group-prefix ffe-cicd/event-api --repo event-api
```

Selects every reference whose `group` starts with `--group-prefix` (a plain string prefix, no
query language) and retags each — one reference's collision failure is reported under `failed`
and never aborts the rest of the batch, same precedent `drop-bulk` already set.

**`portunus tree --by repo`** re-renders the identical tree, keyed on `repo` instead of `group`
— same command, same shape, a second facet on the same underlying data:

```bash
portunus tree --project ffe-cicd --by repo   # --by group is the default, unchanged
```

A reference with no `repo` set renders under a `(no repo set)` bucket, same non-dropping
guarantee `(ungrouped)` already has for `group`. The Project Explorer UI tab has a matching
Group/Repo toggle above its tree view.

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
portunus auth gcp --project my-project-12345   # mint + report identity/scope/expiry only
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
portunus bindings set my-project-12345 --account personal@example.com
portunus bindings show                          # real values -- a local CLI reading your own 0600 config
portunus discover --provider gcp --project ffe-cicd            # uses work@example.com
portunus discover --provider gcp --project my-project-12345  # uses personal@example.com
# both work in the same session, regardless of which account gcloud currently considers active
```

Discovery is read-only and opt-in — it enumerates what already exists in a live GCP Secret
Manager project (names + labels + create-time, **never a value** — `discover.py` holds no
reference to any backend's `access()` method at all) so you register real secrets instead of
re-creating them blind. Worked example, against a real project:

```bash
$ portunus discover --provider gcp --project my-project-12345
not-registered  AUTH_SECRET
not-registered  SANITY_API_ADMIN_TOKEN labels={'project': 'dafshiq1', 'scope': 'admin', 'service': 'sanity', ...}
not-registered  dostal-shared-gemini labels={'app': 'dostal-swarm', 'kind': 'gemini', 'scope': 'shared'}
... (19 secrets total)

$ portunus discover --provider gcp --project my-project-12345 --register
registered  my-project-12345-auth_secret (state=requested)
registered  my-project-12345-sanity_api_admin_token (state=requested)
...
```

Every `--register`ed reference lands in `state=requested` — the same fail-closed placeholder
state agent-initiated `ask "add ..."` requests use — so nothing becomes injectable until a
human reviews and promotes it. The local reference name is derived as `<project>-<sm-name>`
so two different projects that happen to share a secret name never collide.

For a project bound `--sync-mode cached` (see below), `--register` also warms the local
encrypted cache immediately — no separate first-resolve round-trip needed before the value is
available offline. The reference's `state` still stays `requested`: it's not injectable any
sooner, only cached sooner.

### Per-project/per-reference vault routing + sync-down caching

A reference resolves through whichever backend its own project (or the reference itself) is
actually bound to — **not** one global `PORTUNUS_BACKEND` choice for the whole process. Three
levels of precedence: a reference's own `backend` override (set via `portunus_drop`/`reg add`/
`retag --backend {local,gcp,aws}`) wins outright; else the project's `VaultBinding.backend`
(`portunus bindings set <project> --backend ...`); else today's global `PORTUNUS_BACKEND`
env var, unchanged, as the final fallback. This means `my-project-12345` and `ffe-cicd` can
resolve correctly in the *same process* without ever setting `PORTUNUS_BACKEND=gcloud` by hand.

```bash
portunus bindings set gig-tracker --backend local              # everything in this project stays local
portunus bindings set my-project-12345 --backend gcp --sync-mode cached
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
$ portunus sync my-project-12345
  synced        my-project-12345-resend_audience_id
  already-fresh my-project-12345-google_generative_ai_api_key
```

Config lives in `PORTUNUS_HOME/vault-bindings.json` (0600) — the successor to the earlier
`gcp-bindings.json`, read with a migration-safe fallback: if only the legacy file exists, every
entry loads with `backend="gcp", sync_mode="direct"`, byte-for-byte today's real behavior, no
manual migration step.

`account` (a local gcloud identity, e.g. for multi-account setups) and `wif_audience` (the WIF
provider resource name) are also editable straight from Project Explorer's binding panel — the
Standalone UI isn't CLI-shelled-out-only for these anymore. Neither field is a credential
(account is an identity *selector*; the identity itself must already be authenticated locally
via `gcloud auth login`), so neither is masked in the UI.

A rotation binding's `account` (the free-text context hint `portunus rotation-bindings set
<provider> --account ...` already accepted, e.g. a Vercel team slug) is likewise editable inline
in a reference's detail view, next to the Auto-rotate button. `status` (`stub`/`real`) stays
code-driven and is never reachable from the UI — every rotation adapter is a stub today, and a
UI control that could claim otherwise would misrepresent what actually happens on click.

Bulk-import many secrets at once — e.g. importing a batch of candidate passwords/keys before
trying each one against something via `portunus_resolve_exec`, without exposing which one worked
until you check the result:

```bash
portunus drop-bulk entries.json   # [{"name": ..., "sm_name": ..., "value": ...}, ...]
```

A malformed entry is reported under a separate `failed` list and never aborts the rest of the
batch.

### Vault backup: passphrase-locked export/import

`portunus vault export`/`portunus vault import` move the vault's whole critical-state surface
(reference registry, master key, encrypted values, vault bindings, audit log) as one portable
archive — useful for a machine move, a reinstall, or just a real backup that doesn't depend on
a fully-signed desktop app's own local sandboxing.

```bash
export PORTUNUS_EXPORT_PASSPHRASE="something only you know"   # or answer the interactive prompt
portunus vault export --out ~/backups/portunus-$(date +%F).pvault
portunus vault import ~/backups/portunus-2026-08-14.pvault --force   # on a new machine
```

The archive is re-encrypted under your passphrase (PBKDF2-SHA256, 600k iterations) — it never
carries the vault's own live decryption key. A wrong passphrase on import fails closed with a
clear error; the passphrase itself is never accepted via an inline flag, only the
`PORTUNUS_EXPORT_PASSPHRASE` env var or an interactive prompt. `import` refuses to touch a
`PORTUNUS_HOME` that already has vault state present unless you pass `--force` (a full replace,
not a merge). CLI-only by design — no MCP tool, no UI surface: an archive containing every
secret in your vault should never be triggerable by an LLM-facing tool without you directly
running the command yourself.

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
| `portunus_suggest_metadata(name, description="", purpose="", group="", tags=None)` | Propose description/purpose/group/tags — lands in a pending sidecar, **never the live field**; a human confirms via `portunus metadata confirm` or the UI |
| `portunus_crawl_candidates(org="", project="")` | Discovery bundle (sm_name/group/project/org/repo/vault-binding/rotation-binding) for every reference missing metadata — never a value, never a write; read it, then call `portunus_suggest_metadata` for whichever fields you have a real proposal for |
| `portunus_leak_status(name="")` | Already-computed leak severity/finding-count/timestamps for one or every reference with active findings — never a value, never file content, never triggers a scan |
| `portunus_run_leak_scan()` | Runs a real scan over the human-configured leak-scan paths and persists new findings — `{ref_name, path, line_number}` per finding, never a value; can only read paths already added via `portunus_leak_scan_config_add_path` |
| `portunus_leak_scan_config_show()` / `_add_path(glob)` / `_remove_path(glob)` | Manage the leak-scan path config — globs only, never a value, never scans anything itself |
| `portunus_leak_mark_rotated(name)` | Clears a reference's active leak findings — a human/agent assertion Portunus can't independently verify; also invalidates the affected files' scan watermarks so a premature call still gets re-flagged by the next scan |
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
    name="my-project-12345-google_generative_ai_api_key",
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
drawer), Vault Map (second tab, an org → project drill-down — pick an org card, then a project
card, and the view scopes to just that slice, each level showing its own reference count and
completeness summary; a real fix for what becomes an unmanageable flat card wall once a vault
spans 30+ repos), Project Explorer (third tab — a GCP-project-scoped view: what's already
registered, what's discoverable via `portunus discover` with a one-click "register all", and
whether that project has a WIF binding configured), Settings (org/project hierarchy overview +
role/policy management — see "Roles & permissions" below), About (in-app documentation of all
of this), and an Ask Bar (persistent side panel across all four main tabs, backed by `portunus
ask`). A **first-run setup wizard** appears automatically the very first time you open the UI
against a fresh `PORTUNUS_HOME` (never again once anything's configured) — walks through what
Portunus's three parts do, picking your first vault's backend (including a real in-UI trigger
for `gcloud auth login` if you choose GCP), and a first `discover` pass. No gating logic is
duplicated in TypeScript: every
API route under `ui/app/api/` shells out to the same `portunus` console script the CLI uses, so
`Broker.check_injectable` stays the single, only implementation of the gate. The add-secret
form is the one deliberate human-plaintext-entry point (mirroring `portunus drop --stdin`) — a
value is piped to the CLI via stdin only, never an argv element, never logged. A secret's
org/provider/project/env/repo/source_files/description/purpose/injected_as metadata is viewable
and editable from the detail drawer's Move form — never a value, always through the same
`portunus retag` path.

**Missing-metadata signal.** A reference with no `description`/`purpose`/`org`/`project`/`tags`
gets a "⚠ missing metadata" badge everywhere it renders, plus a real clickable Metadata facet
in Console to filter down to exactly what still needs filling in — a real vault checked live
this project has this problem for nearly every reference, so this is a genuine, not
theoretical, gap to close.

**Custom views.** Named, human-curated collections of references for task-shaped clustering
that doesn't map onto org/project/env — "everything for the Shindig deploy," assembled from
wherever those references actually live. Create one from Console's "My views" panel; add/remove
a reference to any view straight from its own detail drawer. CLI: `portunus views create/add/
remove/delete/show`.

**LLM suggests, human confirms.** An agent can propose `description`/`purpose`/`tags`/`group`
for a reference via the `portunus_suggest_metadata` MCP tool — it lands in a pending sidecar,
**never the live field**. `DetailDrawer` shows "suggested by \<agent\>: '...' [Confirm]
[Reject]" for each pending field; Confirm applies it through the exact same `portunus retag`
path a manual edit would use, Reject discards it, neither touches the live field except via
your own explicit click. Routing fields (`org`/`provider`/`project`/`env`/`repo`/`backend`) are
never agent-suggestible — those affect which backend a resolve actually uses. CLI:
`portunus metadata confirm/reject/pending`.

**Roles & permissions — STUB ONLY.** Settings lets you record `{scope: org/project/env, role,
actions}` policy records, and they genuinely persist (`portunus roles set/delete/show`,
`PORTUNUS_HOME/roles.json`) — but nothing reads them yet. `check_injectable()`/`retag()` behave
identically whether or not any policy exists. This is deliberate, staged groundwork for
Petitio's future real access-level enforcement, always shown with an explicit "not yet
enforced" label — never a control that looks live but silently does nothing.

**Metadata crawl & deploy-docs report.** Settings' "Crawl & report" section lists references
still missing description/purpose/org, and offers a "Fetch crawl bundle" button — the same
discovery bundle `portunus crawl --json` returns (sm_name, group, project, org, repo, its
project's vault binding, its provider's rotation binding), framed honestly as **context for an
LLM session to read**, not an automatic filler; nothing in this feature calls an LLM or writes
metadata itself. "View report" renders `portunus report`'s Markdown output in-app (a small
custom renderer, no markdown-parsing dependency — the output is fully controlled and narrow);
"Download report" still saves it as a file — an org → project structure plus an explicit gap
section, useful immediately as a real "deploy docs" starting point, whether or not any
crawl-sourced metadata has ever been confirmed. CLI: `portunus crawl [--org] [--project]
[--json]`, `portunus report [--org] [--project] [--out path]`.

**Leak detection — detective, not preventive.** Settings' "Leak scan" section (and `portunus
leak-scan`) checks whether a managed secret's actual value shows up somewhere it shouldn't —
logs, `.claude` conversation transcripts, shell history, or any other path you explicitly
configure (`portunus leak-scan config add-path <glob>` — nothing is scanned until you add a
path; there is no default). A match escalates a visible severity (warn → urgent → critical)
over time until you run `portunus leak mark-rotated <name>` — a human assertion Portunus
can't independently verify, not a real rotation trigger. This finds secrets that already
leaked; it does not stop the next paste into a chat window, and it never blocks `resolve`/
`inject` (advisory only, matching the roles.json stub's own "detect first, enforce later"
precedent). Findings are always `{reference, file, line}` — never the leaked value itself, at
any layer (CLI, MCP, UI). CLI: `portunus leak-scan [--json]`, `portunus leak-scan config
add-path/remove-path/show`, `portunus leak status [name]`, `portunus leak mark-rotated
<name>`. MCP: `portunus_run_leak_scan`, `portunus_leak_status`, `portunus_leak_scan_config_
show/add_path/remove_path`, `portunus_leak_mark_rotated` — an MCP-connected agent can trigger
a scan (explicit user decision), but only over paths a human already configured; it can never
scan anything outside that set.

**A leak flag follows the reference everywhere, not just in Settings.** A small `⚠ leak:
<severity>` badge (hover: "leaked in N conversations" — distinct files, not raw finding count,
since one transcript can match the same secret on many lines) renders next to a leaked
reference in Console (with its own "Leaked" facet), Vault Map, Project Explorer, and
DetailDrawer — the last of which also shows the full file:line history and a "Mark rotated"
button. Independent from the rotation-requested flag (`RotationBadge`) — an agent-requested
rotation and a leak-detected one are different facts, not the same signal.

### Scheduling leak-scan (cron / CI)

`portunus leak-scan` is cron/CI-ready as-is: it never prompts, and exits `1` when new findings
exist, `0` otherwise.

```bash
# crontab -e
0 9 * * * PORTUNUS_HOME=/path/to/home PORTUNUS_PASSPHRASE_FILE=/path/to/passphrase \
  portunus leak-scan --json >> /path/to/leak-scan.log 2>&1
```

```xml
<!-- ~/Library/LaunchAgents/com.portunus.leakscan.plist (macOS launchd) -->
<key>ProgramArguments</key>
<array>
  <string>/usr/local/bin/portunus</string>
  <string>leak-scan</string>
  <string>--json</string>
</array>
<key>StartCalendarInterval</key>
<dict><key>Hour</key><integer>9</integer><key>Minute</key><integer>0</integer></dict>
```

In a CI pipeline, treat a non-zero exit as a failed step (new leak found) rather than a script
error — check the exit code, not just stderr. Never wire the scheduled run to auto-rotate or
auto-block anything; v1 is deliberately advisory-only (docs/architecture.md §11).

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
  adapters.py    boundary injection adapters (env var, file) -- see Roadmap for HTTP-client
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

<!-- shared:support -->
## Support this project

Free and open source, always. A few ways to help — or just say hi:

- **Use it, star it, file an issue.** Honestly the best support an open-source project can get. → [this project](https://github.com/mdostal/portunus)
- **Hire me.** I do fractional-CTO and consulting work — fixing and scaling tech stacks. → [mdostal.com/contact](https://mdostal.com/contact)
- **[Buy me a coffee](https://www.buymeacoffee.com/mdostal)** if it saved you time.
- **More tools like this** → [tools.mdostal.com](https://tools.mdostal.com)
- **Life outside the terminal** → [life.mdostal.com](https://life.mdostal.com)
- **What we're building at Firefly Events** — event discovery, 8,000+ events/day from 7+ sources → [ff.events](https://ff.events)

Always up for a conversation if any of it's useful to you.
<!-- /shared:support -->
