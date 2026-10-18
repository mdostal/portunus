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

`resolve [text]` and `resolve --stdin` print the **path** of the temp file, not the value, and
the file keeps the template text as given, including the trailing newline `echo` adds. Piping
that output into another command (`... | portunus resolve --stdin | vercel env add`) hands it
the path. When stdout is not a terminal, `resolve` says so on stderr. To give a value to a
command, use `--exec`.

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
| `--keep-whitespace` | Keep leading/trailing whitespace (only the final newline is removed). Implied by `--kind password` |

**Examples:**
```sh
echo "sk-ant-..." | portunus drop anthropic-key dostal-anthropic --provider anthropic --project dostal --stdin
portunus drop stripe-live dostal-stripe-live --value-file /tmp/stripe.txt --project dostal.com --env prod
```

---

### `prompt`

Have the **human** enter a secret through an out-of-band surface and store it directly. Built for agents: the caller can run it from a non-interactive tool channel but only ever gets metadata back, never the value, its length or a hash. Prints one JSON object.

```
portunus prompt <name> <sm_name> [--provider ..] [--project ..] [--kind password] [--enable] [--gate] [--confirm] [--surface auto|kdialog|zenity|osascript|pinentry|konsole|gnome-terminal|xterm|terminal-app|ui] [--timeout 300]
```

| Flag | Description |
|---|---|
| `--kind` | Default `password`, which keeps surrounding whitespace |
| `--description` | Shown to the human in the prompt |
| `--enable` | Set `state=enabled` after storing (default: lands `state=dropped`) |
| `--gate` | Require `portunus approve` before each use |
| `--confirm` | Ask twice in dialog surfaces (terminal windows always ask twice) |
| `--surface` | `auto` (default) picks the first available: `osascript` on macOS; `kdialog`, `zenity`, GUI `pinentry`, then a terminal window on Linux (needs `DISPLAY`/`WAYLAND_DISPLAY`). `ui` creates a `requested` placeholder and opens the dashboard's fulfill page |
| `--timeout` | Seconds to wait for the human |

Plus the metadata flags from `drop` (`--org`, `--project`, `--env`, `--scope`, `--tags`, `--purpose`, `--group`, `--repo`).

Results: `{"name", "state", "stored": true, "gated", "surface"}` (exit 0), `{"cancelled": true}` (exit 2), `{"error": ...}` (exit 1), or for `--surface ui` `{"name", "state": "requested", "stored": false, "pending": true, "url"}` (exit 0). A terminal window passes the value back to `prompt` over a FIFO in a private 0700 directory, never argv, env or a regular file. MCP form elicitation is not a surface, because most clients route the answer through the model.

```sh
portunus prompt sudo-pw dostal-sudo --provider linux --description "sudo on workstation" --enable --gate
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

### `impact`

Show a reference's blast radius: what a rotation of it touches. Metadata only, never a value, never a write.

```
portunus impact <name> [--depth N] [--no-suggest] [--json]
```

Follows `related` transitively in both directions: references this one lists, and references that list it. For every reference it reaches it shows the `project`, `env`, `group`, `repo`, `source_files` and `injected_as` targets, then a flat summary of the projects, repos, files, envs and injection targets involved. A `related` name that is not in the registry is listed as unresolved. `--depth 1` stops at direct links.

The command also lists **suggested related** references: ones that share a source file (same repo or project), the same group, a group parent at least two segments deep, or the same repo, but that aren't linked either way. They are shown with their reason. They are never walked, counted or written. To link one, edit `related` (`portunus retag <name> --related a,b,...` replaces the whole list).

**Example:**
```sh
portunus impact supabase-service-key
portunus impact supabase-service-key --depth 1 --json
```

---

### `inject`

Resolve a reference by tags and inject its value at a boundary target.

```
portunus inject --tags <k=v,...> --target env|file [--var <name>] [--path <path>] [--format env|json|yaml] [--key <key>]
                [--mode 0600|0640|0660] [--group <name|gid>] [--json]
```

A `file` target is written `0600`, owned by the uid Portunus runs as. A caller running as a
different uid can't read that file, and can't delete it unless the directory is writable for
it. To share it, pass `--mode 0640` (or `0660`) and `--group` for a group the caller belongs
to. Portunus can only set a group it is a member of, or the group the file already has from
a setgid directory, so the usual setup is a shared directory with mode `2770` owned by that
group. Mode and group are applied before the value is written. If they can't be applied, the
inject fails and no file is left behind. World-readable and executable modes are rejected.

`--json` prints `{"message": ..., "file": {"path", "uid", "gid", "mode", "readable_by"}}`.
`POST /api/inject` takes `mode` and `group` in the body and returns the same `file` object
next to `ok`. `ok: true` means Portunus wrote the file; check `file` to see who can read it.

A structured key (`scope`, `kind`, `env`, ...) given in `--tags` matches the reference's
structured field. When that field is empty, it matches the same key in the reference's open
`tags` instead.

**Examples:**
```sh
portunus inject --tags provider=vercel,project=mdostal.com --target env --var VERCEL_TOKEN
portunus inject --tags provider=flayr,account=mdostal --target file \
  --path /pantheon-secrets/flayr.env --format env --key FLAYR_API_KEY \
  --mode 0640 --group pantheon-secrets --json
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
portunus bindings set <project> [--backend local|gcp|aws|...] [--sync-mode direct|cached] [--account <email>] [--wif-audience <audience>] [--impersonate-service-account <sa-email>]
portunus bindings show [<project>] [--json]
```

`sync-mode=cached` enables a pull-only local cache; `direct` (default) fetches live on every access.
`--impersonate-service-account` makes every call act as that service account: through the IAM
Credentials API after a WIF mint, or as gcloud's `--impersonate-service-account` alongside
`--account`. See [Keyless GCP access](gcp-keyless.md).

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
portunus approve <name> [--ttl <N | duration>]
```

`--ttl` is either a number of audit-clock ticks (roughly accesses; default 3) or a wall-clock duration such as `90s`, `30m`, `2h` or `1d`. A duration isn't capped by access count, so `portunus approve sudo-pw --ttl 2h` gives a two-hour window.

---

### `grant`

Record an access widening to a named member in the audit log only. No IAM change is made; apply the real permission at the provider yourself.

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
portunus rotation run <ref_name> [--retire-old] [--yes]
```

Before anything runs, it prints the blast radius to stderr ("rotating X touches N other references in M projects", see [`impact`](#impact)). When a real (`auto`) rotation reaches other references through `related`, it prints the full tree and asks `continue? [y/N]`. If stdin is not a terminal it exits 1 without rotating. Pass `--yes` to skip the prompt. A reference with no `related` links in either direction never prompts.

`--retire-old` retires the superseded credential after storing the new one. GCP keys are disabled, never deleted in the same call. Resend keys are deleted, because Resend has no disable (see [rotation.md](rotation.md#resend-api-keys)). Google Cloud API keys are deleted too, but Google can undelete them for 30 days (see [rotation.md](rotation.md#google-cloud-api-keys)).

**Example:**
```sh
portunus rotation run ffe-cicd-sa-key --retire-old
```

---

### `rotation roll`

Rotate a reference and push the new value to every consumer in its `rotation_targets` tag (Convex env, Vercel env + deploy hook, GCP Secret Manager). Then verify every copy, delete the old key, and roll everything back if a push or check fails. Resend only, for now. See [rotation.md](rotation.md#roll-everywhere-portunus-rotation-roll).

```
portunus rotation roll <ref_name> [--dry-run] [--keep-old] [--skip-target SPEC]... [--yes]
```

| Flag | Description |
|---|---|
| `--dry-run` | Print the plan (targets, deploy hooks, checks); change nothing |
| `--keep-old` | Don't delete the old key after a verified roll |
| `--skip-target SPEC` | Leave one `rotation_targets` entry out (repeatable) |
| `--yes` | Skip the confirm prompt; required when stdin is not a terminal |

Prints spec names and key ids only, never a value. It exits 1 if the roll was rolled back, or if
the new key is live everywhere but the old key couldn't be deleted.

---

### `rotate zernio`

Rotate a Zernio webhook signing secret or API key, and push the new value to every env-var target it is injected into (Vercel, Convex) in order: receivers first, Zernio last for a webhook secret; the old key retired last for an API key. See [zernio-rotation.md](zernio-rotation.md).

```
portunus rotate zernio [--webhook <id> | --webhook-secret | --api-key] [--ref <name>] [--env <e>]
                       [--target <spec> ...] [--push-only | --rollback] [--dry-run] [--retire-old]
                       [--no-redeploy] [--yes] [--json]
```

It always runs a read-only preflight first and prints the plan. Without `--yes` it asks before changing anything, and if stdin is not a terminal it exits 1. `--dry-run` stops after the plan. `--push-only` writes the local vault's current value to the targets and mints nothing. `--rollback` (webhook secret only) puts the stashed previous secret back everywhere. Targets come from the `zernio_targets` tag or `--target`: `convex:<deploy-key-ref>:<VAR>` or `vercel:<token-ref>:<project>[@<team>]:<VAR>[:<target>+...]`. A Vercel production target is redeployed and waited on. Any other Vercel target, or any Vercel target with `--no-redeploy`, needs you to confirm a manual redeploy.

**Example:**
```sh
portunus rotate zernio --webhook 6a73653a0ce2dee3dc80b152 --dry-run
```

---

### `rotation audit`

Inventory what is stored vs. what can actually be rotated: group by provider, report `auto`/`manual`/`unknown` capability with reference names and any un-retired superseded key IDs.

```
portunus rotation audit [--json]
```

---

### `liveness check`

Probe stored credentials against their provider's own API with one cheap, read-only call. See [Liveness & Parity](liveness.md#registered-providers) for the registered providers. Exits `0` when all are live, `2` if any is dead, `1` if any check could not reach a verdict. Never prints a value or a response body.

```
portunus liveness check <name> [<name> ...] [--json]
portunus liveness check --all [--json]
```

---

### `liveness status`

Show the last recorded verdict per reference. `--failing` lists only non-live references, oldest failure first.

```
portunus liveness status [--failing] [--json]
```

---

### `parity check`

Compare a reference's value across the places it is supposed to live: its own resolved value,
its copy in GCP Secret Manager or the local vault, an env var on a Convex deployment. Values are
compared as per-call HMACs and never printed; the output says which targets agree. Targets come
from the reference's `parity_targets` tag, or from `--target` (repeatable, replaces the tag).
Exits `0` when every reference is in sync, `2` if any has drifted, `1` if a target could not be
read. See [Liveness & Parity](liveness.md#parity).

```
portunus parity check <name> [<name> ...] [--target SPEC ...] [--json]
portunus parity check --all [--json]
```

`SPEC` is `ref`, `gcp[:<project>]`, `local` or `convex:<deploy-key-ref>[:<VAR>]`.

---

### `rotation-bindings`

Configure per-provider rotation provenance.

```
portunus rotation-bindings set <provider> [--status real|stub|manual] [--account <hint>]
portunus rotation-bindings show [<provider>] [--json]
```

**Example:**
```sh
portunus rotation-bindings set vercel --status real --account my-team-slug
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
If the project's binding (or `--service-account`) names a service account, the federated token is
then exchanged for that service account's token. No gcloud login and no key file are involved.

```
portunus auth gcp [--project <p>] [--audience <audience>] [--service-account <sa-email>]
```

---

### `auth login`

Wrap `gcloud auth login <email>` — the one command to remember when setting up a new machine.

```
portunus auth login <email>
```

---

### `auth status`

Report for each configured GCP binding whether Portunus can get a credential for it right now.
Modes:

- `wif`: reports `ready`, or `no-oidc-token` / `oidc-expired` / `oidc-unreadable` for the
  harness OIDC token. Checked locally, with no network call.
- `account`: `ok`, `missing` (not in `gcloud auth list`), or `EXPIRED`. `EXPIRED` means the
  account is listed but a real token mint fails, e.g. a reauth policy. The status output prints
  the `gcloud auth login <account> --no-activate` fix.
- `ambient`: no account configured, so nothing is checked.

`--strict` exits 1 if any binding is unusable, so you can use it as a gate before a write. No
token is ever printed.

```
portunus auth status [--json] [--strict]
```

---

### `gcp add-version`

Add a new version of an existing Secret Manager secret through the project's binding (WIF →
service account, or `--account` with optional impersonation). The binding's credential is
checked first. The value is read from stdin or a file and passed to gcloud on stdin. It never
appears in argv, output, or the audit log.

```
portunus gcp add-version <project> <secret> (--stdin | --value-file <path>) [--keep-whitespace] [--skip-preflight]
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
portunus audit [N] [--secret <sm_name>] [--since <when>] [--until <when>]
               [--project <project>] [--changes] [--json]
```

`N` is the number of most-recent entries to show (default: 25, or every match inside a
`--since`/`--until` window). `<when>` is a date (`2026-10-15`; a bare date for `--until`
covers that whole day), a datetime (`2026-10-15T09:30`, UTC unless an offset is given), epoch
seconds, or an age (`36h`, `14d`, `2w`). `--project` keeps entries for references whose
project, repo or group root matches. `--changes` keeps only events that change a credential
(`rotate*`, `drop`, `retag`, OAuth/session store and remove, vault imports).

**Example:**
```sh
portunus audit 50 --secret dostal-stripe-live
portunus audit --since 2026-10-10 --until 2026-10-16 --project shop.mdostal.com --changes
```

---

### `diagnose`

Run the audit log backwards from a symptom: "this stopped working around time X, what
changed near then?" Lists every credential change in the window, grouped by reference and
ranked by proximity to `--project`: its own references first, then references linked to them
through `related` or sharing their group root, then everything else. No secret name needed.

```
portunus diagnose --since <when> [--until <when>] [--project <project>] [--check]
                  [--all-actions] [--json]
```

`--check` re-runs `liveness check` on the project and related candidates (all candidates
when no `--project` is given). The run is recorded in the audit log and the liveness status
file. For a candidate that comes back dead, it prints the `injected_as` targets where the
fixed value has to be re-injected. Exit `2` if any re-checked candidate is dead, otherwise `0`.
`--all-actions` includes reads and gates as well as changes.

**Example:**
```sh
portunus diagnose --since 2026-10-10 --project shop.mdostal.com --check
```

---

### `verify`

Verify the audit hash chain. Exits `2` if broken, naming the first bad line of `audit.log`
(a corrupt line is reported, never a crash).

```
portunus verify
```

---

### `health`

Read-only deep self-check. Never resolves a secret value, never writes to `PORTUNUS_HOME`
or the audit log.

```
portunus health [--json]
```

`--json` prints `{"status": "ok"|"degraded"|"down", "checks": [{"name", "ok", "detail"}]}`.
Exit code: `0` ok, `1` degraded, `2` down.

| Check | Fails as | What it proves |
|---|---|---|
| `home` | down | `PORTUNUS_HOME` exists and is a directory |
| `permissions` | degraded | home is `0700` and state files are `0600` |
| `registry` | down | `registry.json` parses and every entry is a valid reference |
| `audit_chain` | degraded | the audit hash chain verifies (same as `portunus verify`) |
| `audit_clock` | degraded | `.clock` equals the last audit `seq`, which catches a truncated log tail the chain alone can't see |
| `bindings` | degraded | `vault-bindings.json` parses (only when present) |
| `backend:<kind>` | degraded | each backend in use: local vault parses and its master key is present and valid; gcloud is on `PATH` and a `versions describe latest` metadata probe on one reference succeeds (WIF-bound references are skipped, since minting writes an audit entry); stub backends fail |

The Docker image runs it as its `HEALTHCHECK`; the UI serves it at `GET /api/health`; the MCP
server exposes it as `portunus_health`.

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
