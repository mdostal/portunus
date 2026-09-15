# CLI Reference

Full reference for the `portunus` CLI. Every command is metadata-only by design — no command ever prints a secret value to stdout; `resolve` either execs a command with the value in argv or writes a 0600 temp file and prints its *path*.

**Global flags** (any command):

| Flag | Description |
|---|---|
| `--home <path>` | Override `PORTUNUS_HOME` for this invocation only |
| `--version` | Print version and exit |

---

## Core resolve/inject

### `resolve`

Resolve a `{{secret:NAME}}` template at the boundary. The value is never printed; it goes into an argv exec or a 0600 temp file.

```
portunus resolve [text]
portunus resolve --stdin
portunus resolve --exec <cmd> [args...]
```

| Flag | Description |
|---|---|
| `text` | Inline template text (e.g. `{{secret:my-token}}`) |
| `--stdin` | Read template from stdin |
| `--exec` | Resolve in argv and exec — `--exec curl -H "x-key: {{secret:my-token}}" https://api.example.com` |

**Example:**
```sh
portunus resolve --exec psql postgres://user:{{secret:db-pass}}@host/db
```

---

### `drop`

Put a new secret into the local-encrypted vault. Lands at `state=dropped`; run `portunus state <name> enabled` to make it injectable.

```
portunus drop <name> <sm_name> [--stdin | --value-file <path>] [flags]
```

| Flag | Description |
|---|---|
| `name` | Reference name (e.g. `shared-anthropic`) |
| `sm_name` | Vault key (e.g. `dostal-shared-anthropic`) |
| `--stdin` | Read value from stdin |
| `--value-file <path>` | Read value from a local file |
| `--provider`, `--project`, `--env` | Routing/tagging fields |
| `--org` | Org umbrella above project |
| `--scope`, `--kind` | Classification metadata |
| `--description`, `--purpose` | Human-readable description fields |
| `--tags` | Comma-separated `k=v` pairs |
| `--group` | Hierarchical path, e.g. `project-y/supabase/auth` |
| `--related` | Comma-separated related reference names |
| `--repo` | Git repo that consumes this secret |
| `--source-files` | Comma-separated file paths in that repo |
| `--backend` | Per-reference backend override (local/gcp/aws/…) |

**Examples:**
```sh
echo "sk-ant-..." | portunus drop anthropic-key dostal-anthropic --provider anthropic --project dostal --stdin
portunus drop stripe-live dostal-stripe-live --value-file /tmp/stripe.txt --project dostal.com --env prod
```

---

### `drop-bulk`

Put many secrets into the local vault at once from a JSON file.

```
portunus drop-bulk <entries_file> [--json]
```

`entries_file` is a JSON array; each entry takes the same fields as `drop` (`name`, `sm_name`, `value` required). A malformed entry is reported without aborting the rest of the batch.

**Example:**
```sh
portunus drop-bulk ./secrets.json --json
```

---

### `state`

Set a reference's lifecycle state.

```
portunus state <name> <state>
```

Valid states: `enabled` | `locked` | `dropped` | `revoked`

**Example:**
```sh
portunus state anthropic-key enabled
```

---

### `list`

List every registered reference for a project — metadata only, never a value.

```
portunus list --project <id> [--provider <p>] [--env <e>] [--json]
```

**Example:**
```sh
portunus list --project mdostal.com --env prod
```

---

### `find`

Find a reference by exact tag match — metadata only.

```
portunus find --tags <k=v,...>
```

**Example:**
```sh
portunus find --tags provider=vercel,project=mdostal.com
```

---

### `search`

Free-text search across all registered secrets — metadata only. Matches against name, sm_name, description, purpose, tags, and group path.

```
portunus search <query> [--project <p>] [--provider <p>] [--env <e>] [--state <s>] [--json]
```

**Example:**
```sh
portunus search stripe --project dostal.com --env prod
```

---

### `tree`

Render secrets by group hierarchy and related links — metadata only.

```
portunus tree [--project <p>] [--by group|repo] [--json]
```

`--by group` (default) nests by the free-text group path; `--by repo` nests by the structured repo field.

**Example:**
```sh
portunus tree --project ffe-cicd --json
```

---

### `inject`

Resolve a reference by tags and inject its value at a boundary target.

```
portunus inject --tags <k=v,...> --target env|file [--var <name>] [--path <path>] [--format env|json|yaml] [--key <key>]
```

**Example:**
```sh
portunus inject --tags provider=vercel,project=mdostal.com --target env --var VERCEL_TOKEN
```

---

### `ask`

Semantic front door: natural-language request → resolved injection. Fails closed on ambiguous or unrecognized requests.

```
portunus ask "<request>" [--target env|file] [--var <name>] [--path <path>] [--name <n>] [--tags <k=v,...>] [--json]
```

Omit `--target` to preview the resolved reference without injecting. For an **add** request, `--name` and `--tags` are required. For a **rotate** request, Portunus flags the reference for human action.

**Examples:**
```sh
portunus ask "the vercel token for mdostal.com in prod" --target env --var VERCEL_TOKEN
portunus ask "stripe prod secret for dostal.com"   # preview only
portunus ask "add a new stripe key" --name stripe-live --tags provider=stripe,project=dostal.com,env=prod
```

---

## Registry management

### `reg`

Manage the reference registry directly.

```
portunus reg show
portunus reg add <name> <sm_name> [--scope <s>] [--kind <k>] [--org <o>] [--project <p>] [--description <d>] [--purpose <p>] [--tags <k=v,...>] [--group <g>] [--related <r,...>] [--repo <repo>]
portunus reg rm <name>
portunus reg json
```

**Example:**
```sh
portunus reg add linear-api dostal-linear --provider linear --project mdostal.com --description "Linear API key" --purpose "CI issue sync"
```

---

### `retag`

Update a reference's routing/metadata in place — never touches a value.

```
portunus retag <name> [--org <o>] [--provider <p>] [--project <p>] [--env <e>] [--tags <k=v,...>] [--description <d>] [--purpose <p>] [--group <g>] [--related <r,...>] [--repo <repo>] [--source-files <f,...>]
```

**Example:**
```sh
portunus retag linear-api --group ffe-cicd/notifications --source-files src/ci/notify.ts
```

---

### `retag-bulk`

Retag every reference whose `group` starts with a prefix — useful for backfilling `repo` and `source_files` across many references at once.

```
portunus retag-bulk --group-prefix <prefix> [--org <o>] [--repo <r>] [--source-files <f,...>] [--dry-run] [--json]
```

**Example:**
```sh
portunus retag-bulk --group-prefix ffe-cicd/ --repo firefly-events/flayr --dry-run
```

---

## Vault management

### `vault status`

Check whether this `PORTUNUS_HOME` has ever been initialized.

```
portunus vault status [--json]
```

---

### `vault export`

Export a passphrase-locked snapshot of the vault (registry, master key, encrypted values, bindings, audit log). CLI-only — never triggerable via MCP.

```
portunus vault export [--out <path>]
```

The passphrase is read from `PORTUNUS_EXPORT_PASSPHRASE` or interactively (prompted twice).

**Example:**
```sh
portunus vault export --out ~/backups/portunus-$(date +%Y%m%d).pvault
```

---

### `vault import`

Restore a vault export. Refuses an existing vault unless `--force`.

```
portunus vault import <archive> [--force]
```

---

### `vault access export`

Export a scoped, plain-JSON metadata bundle (registry + bindings, **no secret values**) for transferring access info to another Portunus instance.

```
portunus vault access export [--project <p>] [--org <o>] [--tags <k=v,...>] [--out <path>]
```

---

### `vault access import`

Import a scoped access bundle from another instance.

```
portunus vault access import <bundle> [--force]
```

---

### `vault access verify`

Run a real per-reference reachability check across the registry.

```
portunus vault access verify [--project <p>]
```

---

### `sync`

Force a recency check (and re-fetch if stale) for every `sync_mode=cached` reference in a project. Useful before a deploy to materialize a fresh set of secrets.

```
portunus sync <project> [--json]
```

**Example:**
```sh
portunus sync ffe-cicd
```

---

### `bindings`

Configure per-project vault bindings (which backend serves a project's secrets).

```
portunus bindings set <project> [--backend local|gcp|aws|...] [--sync-mode direct|cached] [--account <email>] [--wif-audience <audience>]
portunus bindings show [<project>] [--json]
```

`sync-mode=cached` enables a pull-only local cache; `direct` (default) fetches live on every access.

**Example:**
```sh
portunus bindings set ffe-cicd --backend gcp --sync-mode cached --account me@example.com
portunus bindings show
```

---

## Access control

### `gate`

Require an explicit approval before a reference can be resolved.

```
portunus gate <name> [--off]
```

---

### `approve`

Grant a time-boxed approval for a gated reference.

```
portunus approve <name> [--ttl <N>]
```

`--ttl` is the number of accesses to allow (default: 3).

---

### `grant`

Record an audited access widening to a named member.

```
portunus grant <name> <member>
```

---

### `roles`

Configure RBAC policy records. **Audit-only for now** — policies are evaluated on every resolve and logged, but not enforced (raised on) unless `roles enforce on` is set.

```
portunus roles set --scope-type project|org|env|provider --scope-value <v> --role <r> [--actions <a,...>] [--principal <p>]
portunus roles delete --scope-type <t> --scope-value <v> --role <r> [--principal <p>]
portunus roles show [--scope-type <t>] [--scope-value <v>] [--json]
portunus roles enforce on|off|status
```

**Example:**
```sh
portunus roles set --scope-type project --scope-value dostal.com --role owner --actions read,prod-release
portunus roles enforce on
```

---

## MCP / agent

### `mcp`

Start the Portunus MCP stdio server for other agents and harnesses.

```
portunus mcp
```

---

### `agent init`

Wire the MCP server and install usage skills into every detected agent CLI on this machine (Claude Code, Codex CLI). Idempotent — safe to re-run.

```
portunus agent init [--harness claude|codex] [--json]
```

**Example:**
```sh
portunus agent init
portunus agent init --harness claude
```

---

### `agent status`

Show what's currently wired — never mutates anything.

```
portunus agent status [--json]
```

---

## Rotation

### `rotation run`

Run a real rotation for a single reference: create → verify → store cycle. Only works for providers with an `auto` capability adapter.

```
portunus rotation run <ref_name> [--retire-old]
```

`--retire-old` disables the superseded credential after storing the new one (never deletes in the same call).

**Example:**
```sh
portunus rotation run ffe-cicd-sa-key --retire-old
```

---

### `rotation audit`

Inventory what is stored vs. what can actually be rotated: group by provider, report `auto`/`manual`/`unknown` capability with reference names and any un-retired superseded key IDs.

```
portunus rotation audit [--json]
```

---

### `rotation-bindings`

Configure per-provider rotation provenance.

```
portunus rotation-bindings set <provider> [--status real|stub|manual] [--account <hint>]
portunus rotation-bindings show [<provider>] [--json]
```

**Example:**
```sh
portunus rotation-bindings set vercel --status stub
portunus rotation-bindings show
```

---

## OAuth

### `oauth store`

Store an OAuth credential bundle. The credential JSON comes from stdin or a file, never an inline flag.

```
portunus oauth store <provider> <account> --stdin | --value-file <path>
```

**Example:**
```sh
cat ~/.config/oauth-google.json | portunus oauth store google me@example.com --stdin
```

---

### `oauth list`

List every stored OAuth credential's metadata — never a credential field.

```
portunus oauth list [--json]
```

---

### `oauth remove`

Remove a stored OAuth credential.

```
portunus oauth remove <provider> <account>
```

---

## Session

Browser/login session storage. Session JSON contains live cookies/tokens and receives the same 0600-tempfile treatment as secret values.

### `session store`

```
portunus session store <site> <account> --ttl-seconds <N> --stdin | --value-file <path> [--rotation-interval-seconds <N>] [--org <o>] [--project <p>] [--env <e>] [--repo <r>]
```

### `session load`

Writes a 0600 temp file and prints only the path.

```
portunus session load <site> <account> [--allow-expired]
```

### `session inspect`

Show session metadata only — never the payload.

```
portunus session inspect <site> <account> [--json]
```

### `session list`

List every stored session's metadata.

```
portunus session list [--json]
```

### `session remove`

```
portunus session remove <site> <account>
```

---

## GCP

### `auth gcp`

Mint a GCP Workload Identity Federation access token and report identity/scope/expiry — never the token.

```
portunus auth gcp [--project <p>] [--audience <audience>]
```

---

### `auth login`

Wrap `gcloud auth login <email>` — the one command to remember when setting up a new machine.

```
portunus auth login <email>
```

---

### `auth status`

Cross-reference every configured GCP binding's account against `gcloud auth list`.

```
portunus auth status [--json]
```

---

### `discover`

Read-only: list what already exists in a live GCP Secret Manager project (names + labels, never values). With `--register`, writes not-yet-registered entries as `state=requested` placeholders.

```
portunus discover --provider gcp --project <gcp-project-id> [--register] [--json]
```

**Example:**
```sh
portunus discover --provider gcp --project my-gcp-project
portunus discover --provider gcp --project my-gcp-project --register
```

---

## UI / desktop

### `ui open`

Open the vault web dashboard in a browser. Fire-and-forget — safe for an agent's own tool call. Set `PORTUNUS_UI_URL` to override the default `http://localhost:3000`.

```
portunus ui open [--fulfill <ref_name>]
```

`--fulfill <ref_name>` deep-links straight into the pre-filled Fulfill form for a `state=requested` reference.

**Example:**
```sh
portunus ui open --fulfill stripe-live
```

---

## Maintenance

### `audit`

View the tamper-evident access log. Entries are hash-chained; use `verify` to confirm integrity.

```
portunus audit [N] [--secret <sm_name>] [--json]
```

`N` is the number of most-recent entries to show (default: 25).

**Example:**
```sh
portunus audit 50 --secret dostal-stripe-live
```

---

### `verify`

Verify the audit hash chain. Exits `2` if broken.

```
portunus verify
```

---

### `status`

Show a reference's current state and approval gate.

```
portunus status <name>
```

---

### `crawl`

Bundle known context for references missing description/purpose/org — for an LLM or human to review and then call `metadata confirm` or `portunus_suggest_metadata` against.

```
portunus crawl [--org <o>] [--project <p>] [--json]
```

---

### `report`

Render current vault state as Markdown — a deploy-docs starting point.

```
portunus report [--org <o>] [--project <p>] [--out <path>]
```

---

### `metadata`

Confirm or reject agent-suggested metadata fields (the human-review counterpart to the `portunus_suggest_metadata` MCP tool).

```
portunus metadata confirm <name> <field>
portunus metadata reject <name> <field>
portunus metadata pending [--json]
```

Valid fields: `description`, `purpose`, `group`, `tags`.

---

### `views`

Named, human-curated reference collections for ad-hoc task clustering.

```
portunus views create <name> [--description <d>]
portunus views add <name> <ref_name>
portunus views remove <name> <ref_name>
portunus views delete <name>
portunus views show [<name>] [--json]
```

---

### `leak-scan`

Scan configured local paths for occurrences of managed secret values. Advisory only — never blocks resolve or injection.

```
portunus leak-scan [--json]
portunus leak-scan config add-path <glob>
portunus leak-scan config remove-path <glob>
portunus leak-scan config show [--json]
portunus leak-scan config add-repo <repo_path>
portunus leak-scan config remove-repo <repo_path>
portunus leak-scan config show-repos [--json]
```

Exits non-zero when new findings are found (useful in CI/cron).

**Example:**
```sh
portunus leak-scan config add-path "~/.claude/projects/**/*.jsonl"
portunus leak-scan
```

---

### `leak`

Query and manage per-reference leak-scan findings.

```
portunus leak status [<name>] [--detail] [--json]
portunus leak mark-rotated <name>
```

`mark-rotated` is a human assertion that the secret has been rotated at its provider and clears active findings.

---

### `update`

Self-update the CLI. Checks GitHub releases; never a silent unattended install.

```
portunus update check [--json]
portunus update run [--yes]
```

`--yes` skips the interactive confirmation (for scripts/cron).
