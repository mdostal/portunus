# HTTP routes and the write credential

The standalone UI (`ui/`) is the only part of Portunus that listens on a network port. The CLI
changes the vault in-process. `portunus mcp` speaks MCP over stdio and also changes the vault
in-process. Neither one sends a request to the UI (`tests/test_write_credential_scope.py` checks
this), so the write credential below does not apply to them.

## The write credential

Every request to `/api/*` whose method is not `GET` or `HEAD` must carry the UI's write
credential:

```
X-Portunus-Write-Token: <token>
```

The server reads the expected token from the file named by `PORTUNUS_WRITE_TOKEN_FILE`, with
surrounding whitespace trimmed. The token must be at least 16 characters long. The file is read
again on every write, so to rotate the token you rewrite the file. No restart is needed.

`ui/proxy.ts` enforces the credential before any route handler runs, and so before any
`portunus` CLI process starts. The comparison is constant-time: both sides are hashed with
SHA-256 and the digests are compared with `timingSafeEqual`. The check covers every method other
than GET and HEAD on every `/api/*` path. A route added later is guarded without anyone listing
it, and so is a path no route serves.

| Server environment | Write without a valid token | Write with the token |
|---|---|---|
| `PORTUNUS_WRITE_TOKEN_FILE` names a readable file holding a token of 16+ characters | **401** `{"code": "write_token_required"}` | allowed |
| `PORTUNUS_WRITE_TOKEN_FILE` is set, but the file is missing or unreadable, or its token is shorter than 16 characters | **503** `{"code": "write_token_unavailable"}` | **503** |
| Neither variable is set (the default) | **503** `{"code": "write_token_unconfigured"}` | **503** |
| `PORTUNUS_ALLOW_UNAUTHENTICATED_WRITES=1` and no token file | allowed (the behaviour before the credential existed) | allowed |

A token file takes precedence over the opt-out. When writes are refused or open, the server
prints a banner at startup. The deep `GET /api/health` also reports a `ui-write-auth` check.
That check fails when writes are refused, and the overall status is then at least `degraded`.
When writes are open, its detail starts with `OPEN:`. The token itself is never logged or
returned.

The checks that already existed still run first:

1. A foreign `Origin` gets 403.
2. A non-loopback `Host` gets 403, unless `PORTUNUS_UI_HOST` is set.
3. A `Content-Type` other than JSON gets 415.

These checks stop a hostile web page. They do nothing against a client that sets its own
headers. That includes a container that reaches the host's loopback port, another local user,
or a script. The write credential is what stops those clients.

### The UI in a browser

`ui/instrumentation-client.ts` adds the header to the page's own same-origin `/api/*` writes. The
browser gets the token in one of two ways:

- **From a `#write-token=<token>` URL fragment.** The page stores the token in `sessionStorage`
  for that tab only and removes it from the address bar. A browser never sends a fragment to the
  server, and the server never logs one. The desktop app opens its window this way.
- **From a prompt.** If the page has no token, its first write gets a 401. The page then opens a
  password dialog and retries the write once with the token you enter. If the stored token is
  rejected, the page forgets it and asks again.

### Standalone setups

**A single-user machine, with the credential (recommended):**

```bash
umask 077
openssl rand -hex 32 > ~/.portunus/ui-write-token
cd ui && PORTUNUS_WRITE_TOKEN_FILE=~/.portunus/ui-write-token npm run dev
cat ~/.portunus/ui-write-token   # paste it when the UI first asks
```

**A single-user machine, without the credential.** Use this only where nothing else can reach
the port:

```bash
PORTUNUS_ALLOW_UNAUTHENTICATED_WRITES=1 npm run dev
```

Any process that can connect to the port can then change the vault. That includes other users
on the machine, and containers that can reach the host's loopback port.

**The desktop app** needs no setup. Each launch mints a fresh 32-byte token, writes it to
`ui-write-token` (mode 0600) in the app's data directory, and passes that path to its UI as
`PORTUNUS_WRITE_TOKEN_FILE`. The app opens its window with the token in the URL fragment and
deletes the file when it quits. If the token cannot be minted, the UI refuses writes. It never
falls back to open writes.

**A container, or a service that other services call:** mount the token as a secret file, for
example with compose `secrets:`. Give it only to the UI's own container and to the services
that need to write. Set `PORTUNUS_WRITE_TOKEN_FILE` to the mounted path, and have each writer
send the header.

### Rollout for a deployment that already has HTTP writers

1. **Mint the token.** Run `openssl rand -hex 32` and put the output in a file. Mount that file
   into the UI's container, and into every service that calls a Portunus write route.
2. **Update the callers first.** Each caller reads the same file and sends
   `X-Portunus-Write-Token` on every POST. An older Portunus ignores the header, so this step is
   safe to deploy on its own.
3. **Then upgrade Portunus** with `PORTUNUS_WRITE_TOKEN_FILE` set. If you upgrade before the
   file is mounted, every write fails with 503, and the 503 says why. Reads keep working.

A caller that has no token gets 401 from then on.

## Every route, classified

This table lists every HTTP route the UI serves. Its classes are:

- **state-changing**: the route changes the vault, its configuration, or the host. It requires
  the credential.
- **metadata**: the route returns names, tags, state, or paths. It never returns a secret value.
- **secret value**: the route returns a secret value in its HTTP response.

No route is in the **secret value** class. No route returns a secret value over HTTP.

| Route | Method | Class | What it does |
|---|---|---|---|
| `/api/approvals` | GET | metadata | Approval-gate status per gated reference (`portunus approvals --json`): gated, approved, time box (`expires_at` or `ticks_left`), pending. `?project=` narrows it to one project. |
| `/api/approve` | POST | state-changing | Approves a gated reference for a time box: `{name, ttl}` runs `portunus approve <name> --ttl <ttl>` (`15m`, `2h`, `1d`, or a bare count of accesses). |
| `/api/ask` | POST | state-changing | Resolve-only preview of a natural-language request; returns the matched reference's metadata. A "rotate" request tags the reference `rotation_requested=true` (a retag). |
| `/api/audit` | GET | metadata | Audit trail entries (actor, action, reference name, result). |
| `/api/auth-login` | POST | state-changing | Runs `gcloud auth login <email>` on the server host (a browser OAuth flow). |
| `/api/auth-status` | GET | metadata | Vault bindings and whether each account is credentialed in gcloud. Runs `gcloud auth list`. |
| `/api/bindings` | GET | metadata | Vault bindings: backend, sync mode, account, WIF audience (none is a credential). |
| `/api/bindings` | POST | state-changing | Sets a project's vault binding. |
| `/api/crawl` | GET | metadata | Discovery bundle for references missing metadata. |
| `/api/discover` | GET | metadata | Diff of a GCP project's Secret Manager secret names against the registry. Calls the GCP API with the server's credentials. |
| `/api/discover` | POST | state-changing | Registers the discovered references. |
| `/api/drop` | POST | state-changing | Stores a secret value sent in the request body (piped to the CLI over stdin, never echoed back). |
| `/api/gate` | POST | state-changing | Turns a reference's approval gate on or off: `{name, on}` runs `portunus gate <name> [--off]`. |
| `/api/health` | GET | metadata | Deep self-check (`portunus health`), the Host check and the write-credential check. `?shallow=1` is liveness only. |
| `/api/impact` | GET | metadata | A reference's blast radius. |
| `/api/inject` | POST | state-changing | Resolves a secret and writes its **value to a file at a caller-chosen path** (or into the env of the short-lived CLI process, which then exits). The response carries the file's owner/group/mode, never the value. A file target must not exist yet and is never written through a symlink (see below). |
| `/api/leak-scan` | POST | state-changing | Runs a leak scan; persists findings and watermarks. |
| `/api/leak-scan-config` | GET | metadata | Configured scan paths and repos. |
| `/api/leak-scan-config` | POST | state-changing | Adds/removes a scan path or repo. |
| `/api/leak-status` | GET | metadata | Leak findings: file and line, never the matched text. |
| `/api/leak-status` | POST | state-changing | Marks a reference rotated. |
| `/api/list` | GET | metadata | References in a project. |
| `/api/metadata` | POST | state-changing | Confirms (applies via retag) or rejects a suggested metadata field. |
| `/api/oauth-store` | GET | metadata | `?detect=adc`: whether the server user's gcloud ADC file is usable (a boolean only). |
| `/api/oauth-store` | POST | state-changing | Stores an OAuth credential bundle (from the body, or from the server user's ADC file with `use_local_adc`) and registers its reference. |
| `/api/registry` | GET | metadata | The whole registry (every reference's metadata). `?project=<name>` returns only that project's references (matched on project, repo or group root). This narrows the view; it is not an access boundary. |
| `/api/report` | GET | metadata | Markdown deploy-docs report. |
| `/api/retag` | POST | state-changing | Changes a reference's routing fields and tags. |
| `/api/roles` | GET | metadata | Policy records. |
| `/api/roles` | POST | state-changing | Sets/deletes a policy record. |
| `/api/rotation-status` | GET | metadata | Rotation bindings per provider. |
| `/api/rotation-status` | POST | state-changing | Sets a provider's rotation `account` hint. |
| `/api/search` | GET | metadata | Metadata search. |
| `/api/state` | POST | state-changing | Sets a reference's lifecycle state: `{name, state}` with state one of enabled, locked, dropped, revoked; runs `portunus state <name> <state>`. |
| `/api/vault-status` | GET | metadata | Whether a vault is configured (first-run detection). |
| `/api/views` | GET | metadata | Custom views (reference names). |
| `/api/views` | POST | state-changing | Creates/edits/deletes a view. |
| `/` and Next.js assets | GET | none | The app shell; no vault data. |

Reads stay open by design. A caller that can reach the port can still read the following
without a credential:

- the whole registry: names, tags, secret-manager paths, and the audit trail
- leak findings, including where a value leaked. These are file paths and line numbers, not the
  value.
- GCP Secret Manager names. Getting these makes the server call GCP with its own credentials
  (`/api/discover`, `/api/auth-status`).

If that is too much for your deployment, do not expose the port to such callers. A reverse
proxy that requires auth on GET also works.

## File targets never overwrite and never follow a link

`inject --target file` creates the file with `O_CREAT|O_EXCL|O_NOFOLLOW`. If anything already
exists at the path (a regular file, a directory, a symlink, or a dangling symlink), the kernel
refuses in one step and the inject fails. Nothing is written. The file is created at its final
name, never under a temp name that is then renamed:

- a rename would replace whatever is at the target
- if the UI kills an abandoned inject (PANT-1003), a leftover temp file would hold the value
  under a name nobody cleans up

Group and mode are applied before the value is written. On failure the file the call created
is removed. A file that existed before the call is never touched.

To refresh a file you own, the CLI takes `--replace`. It still refuses a symlink. It writes a
fresh temp file next to the target (`mkstemp`: `O_EXCL`, so it never follows a link) and
`os.replace()`s it over the target. That swaps the directory entry and never writes through it.
The HTTP route never forwards `--replace`.
