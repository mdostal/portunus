# Changelog

All notable changes to Portunus are documented in this file.

## [Unreleased]

### Added

- **APNs auth-key liveness and mobile credential runbook (PANT-1127, #202).** A new `apns`
  liveness probe signs an ES256 provider JWT with the stored `.p8` and sends it to APNs over
  HTTP/2. The push targets a device token that can't exist, as a silent `background` push with
  `apns-expiration: 0`, so nothing is delivered. `403 InvalidProviderToken` reads `dead`, and a
  device-token refusal (`400 BadDeviceToken`) reads `live`. Environment or topic mismatches,
  429 and 5xx read `unknown`. It needs the `apns_team_id` tag and a key ID (the `apns_key_id`
  tag, or the name suffix as in `apns-authkey-B9FVCQ4GB4`). It also needs the new optional
  `pantheon-portunus[apns]` extra (`h2`). The new `docs/mobile-credentials-runbook.md` covers
  the manual re-issue of APNs keys, App Store Connect API keys, Apple code-signing certificates
  and profiles, and Android keystores. It's linked from the rotation matrix and `rotation.md`.
  No rotation adapters: none of these has a provider API.
- **Keyless GCP access for Secret Manager writes (PANT-1171, #231).** WIF bindings can now
  impersonate a least-privilege service account. After the STS exchange, `GCPWorkloadIdentityAuth`
  calls the IAM Credentials API `generateAccessToken` (`service_account=`), so writes need no
  interactive `gcloud` login and no key file. `bindings set` gains
  `--impersonate-service-account`, and `auth gcp` gains `--service-account` and reports which
  service account it minted for. `auth status` now probes each `account` binding with a real token
  mint, so a listed account stuck behind a reauth wall shows `EXPIRED` along with the
  `gcloud auth login <account> --no-activate` fix. WIF bindings report the readiness of the OIDC
  token, and `--strict` exits 1 when anything is unusable. The new
  `portunus gcp add-version <project> <secret> --stdin|--value-file` writes a version through the
  binding after that preflight, passing the value to gcloud on stdin only. The new
  `docs/gcp-keyless.md` runbook covers setting this up.
- **Vercel token liveness (PANT-1149, #144).** `portunus liveness check` now probes `vercel`
  references with `GET https://api.vercel.com/v5/user/tokens/current`. This is the token's own
  metadata, read-only and with no project or deployment data. Vercel answers every auth failure
  with 403, including a request with no token, so only 401 or a 403 carrying
  `error.invalidToken: true` reads `dead`. Any other 403 reads `unknown`. The Vercel transport
  reads the response body only to pull out that flag and the token's `expiresAt`, and nothing
  else from the body leaves it. A live token shows its expiry date. Within 14 days of expiry
  the detail warns and names `portunus rotation run <name>`, because a Vercel token can only
  be rotated while it still works. A deploy token stored under `gcp` needs the
  `liveness_provider=vercel` tag.
- **Reverse rotation diagnosis (PANT-1121, #208).** Audit entries now carry a wall-clock `ts`
  (epoch seconds, covered by the hash). Entries written before this have none and still verify.
  `portunus audit` gains `--since`/`--until` (dates, datetimes, epoch seconds or ages like
  `14d`), `--project` and `--changes`, so you can query the log by time window without knowing
  which secret to filter on. The new `portunus diagnose --since <when> [--project P] [--check]`
  groups the window's credential changes by reference and ranks them: the project's own
  references, then `related`/same-group ones, then everything else. `--check` re-runs liveness
  on the nearest candidates and, for dead ones, lists their `injected_as` re-injection targets.
  The full re-inject-everywhere walk waits on the dependency graph (#207).
- **Reference blast radius: `portunus impact` (PANT-1122, #207).** `portunus impact <name>` and the
  `portunus_impact` MCP tool follow `related` transitively in both directions. They render every
  reference reached, with its project, env, repo, group, `source_files` and `injected_as` targets,
  plus a summary of the projects, repos, files and injection targets involved. Unresolved `related`
  names are listed rather than failing. A separate *suggested related* list proposes unlinked
  references that share a source file, group, group parent or repo, with the reason for each.
  Suggestions are computed on read, never written to `related`, and never walked or counted.
  `rotation run` now prints the blast radius to stderr before it runs. When a real rotation reaches
  other references it asks `continue? [y/N]`. Without a terminal it refuses unless `--yes` is
  passed. The UI detail drawer gains a blast-radius panel (tree plus summary) above the Rotate
  buttons, served by `/api/impact`.

- **Event-source API liveness, provider backfill and runbooks (PANT-1126, #203).** Liveness
  probes for `eventbrite` (private token), `ticketmaster` (consumer key) and `seatgeek`
  (client id), plus a `meetup` adapter that explains why nothing Meetup stores can be probed.
  The new `CredentialKindLivenessAdapter` probes only the credential kinds that can be sent on
  their own (`liveness_kind` tag, or the reference name's suffix). Client secrets and similar
  read `unknown` with no request, and `liveness check --all` skips them. `HTTPProbe` gains
  `query_param` for providers that take the key in the URL; the value never reaches a result.
  `scripts/backfill_event_api_providers.py` sets the real `provider` on
  `event-api-*-{meetup,eventbrite,ticketmaster,seatgeek}-*` (dry run by default). Ticketmaster
  and SeatGeek move from `unknown` to `manual` in `provider-rotation-matrix.md`, and
  `rotation.md` gains manual re-issue runbooks for all four.
- **GCP service-account key storage formats (PANT-1124, #205).** `GCPServiceAccountKeyRotationAdapter`
  now reads `tags["gcp_key_format"]`: `json` (default, unchanged), `base64` (stores base64 of the
  key file and decodes it to find the old key id), or `private_key` (stores only the PEM, for split
  `*-private-key` / `*-client-email` refs; the old key id comes from `tags["gcp_key_id"]`, which is
  rewritten after each store). Before this, rotating a `*-base64` ref overwrote it with raw JSON, and
  `--retire-old` silently disabled nothing. An unknown format, or `private_key` with `--retire-old`
  but no `gcp_key_id`, is refused before any gcloud call.
- **Agent-triggered secure prompt (PANT-1119, #210).** `portunus prompt <name> <sm_name>`, the
  `portunus_prompt` MCP tool and a `portunus-prompt` agent skill let an agent ask the human to
  type a secret (a sudo password, a master credential) into a desktop password dialog
  (`osascript`, `kdialog`, `zenity`, GUI `pinentry`) or a new terminal window, and store it
  directly. The agent only gets metadata back (`{"name", "state", "stored": true}` or
  `{"cancelled": true}`), never the value, its length or a hash. `--enable`/`--gate` set the
  reference up for use in the same step; `--surface ui` opens the dashboard's fulfill page instead.
- **Wall-clock approvals.** `portunus approve <name> --ttl 2h` (also `90s`, `30m`, `1d`) grants a
  time window instead of audit-clock ticks. A bare number keeps its old meaning.
- **`drop --keep-whitespace`**, implied by `--kind password`: only the final newline is removed,
  so a password with leading or trailing spaces is stored exactly.

- **Liveness probes for long-tail single-key SaaS providers (PANT-1125, #204).** New built-in
  probes for `browserbase`, `firecrawl`, `healthchecks`, `pexels`, `pixabay`, `serpapi`, `shippo`,
  `tremendous` and `vercel-ai`, so `portunus liveness check --all` now finds dead keys for these
  providers too. Each probe is one cheap authenticated read, and only an explicit credential
  rejection reads `dead`. `HTTPProbe(query_param=...)` covers providers that only accept the key
  in the query string (Pixabay, SerpAPI) and keeps it out of results. Tremendous picks the sandbox
  or production host from the `TEST_`/`PROD_` key prefix or the `tremendous_env` tag. The default
  liveness transport now sends a `portunus-liveness` User-Agent, because Pexels and Pixabay reject
  urllib's default with 403. `docs/liveness.md` explains why Fly.io, stack-scoped Grafana Cloud,
  Qdrant Cloud and Slack still have no probe.

- **Neon Postgres liveness + role-password rotation (PANT-1120, #209).** New `neon` liveness probe
  connects with each stored connection string and runs `SELECT 1`. SQLSTATE class 28 (`28P01`
  password authentication failed) reads `dead`, and network, TLS and endpoint errors read
  `unknown`. It would have caught the 2026-10-16 incident, where Neon's dev-branch password changed
  server-side and `shop-mdostal-database-url-dev` sat broken. It uses a new driverless Postgres
  client, `portunus.pgwire` (TLS with certificate verification, SCRAM-SHA-256 with channel binding,
  MD5, cleartext), so there's no new dependency. New `NeonRotationAdapter` (provider `neon`) resets
  the role password through the Neon API, stores the connection string with only its password
  replaced, waits for Neon's operations to finish and verifies it with a real connect. A reset
  retires the old password by design, so `--retire-old` is required. Neon is now in
  `docs/rotation.md` and `docs/provider-rotation-matrix.md`.

- **Google Cloud API key rotation (PANT-1136, #193).** New `GoogleCloudAPIKeyRotationAdapter`,
  registered as `google-api-key` (separate from `gcp`, which rotates service-account JSON keys).
  It runs via `portunus rotation run <ref> [--retire-old]`. It finds the old key (from
  `tags["google_api_key_name"]` or `keys:lookupKey`), creates a new one with the same
  restrictions, checks it with `keys:lookupKey` and, if `tags["google_api_key_verify_url"]` is
  set, a live read-only call. Then it stores it and writes the new key name to
  `tags["google_api_key_name"]`. `--retire-old` deletes the old key, which Google can undelete for
  30 days. It refuses a stored value that is a service-account JSON key, so an ambiguous reference
  such as `flayr-vertex-ai-*-key` can't be rotated as the wrong kind. Needs
  `apikeys.googleapis.com` enabled and `roles/serviceusage.apiKeysAdmin` (or the same
  permissions) on the calling identity.

- **Vercel access token rotation (PANT-1135, #194).** `VercelRotationAdapter` (provider `vercel`)
  is no longer a stub: it creates a replacement token (`POST /v3/user/tokens`), verifies it with
  a read-only `GET /v5/user/tokens/current` that must return the new id, stores it, and with
  `--retire-old` deletes the old token (`DELETE /v3/user/tokens/{id}`). The old token's id comes
  from `tags["vercel_token_id"]` or is looked up with the stored token. Optional tags set a team,
  a project scope and an `expiresAt` TTL. This is proactive rotation only: a token that is already
  dead can't authenticate the create call, which is the liveness problem (#144). Vercel AI API
  keys are a different product and stay out of scope. See `docs/rotation.md`.
- **Grafana Cloud access policy token rotation (PANT-1137, #192).** `GrafanaCloudRotationAdapter`
  (provider `grafana-cloud`) creates a new token under the same access policy, verifies it with a
  read-only GET, stores it, and with `--retire-old` deletes the old token. It needs the
  `grafana_access_policy_id` and `grafana_region` tags; see `docs/rotation.md`. Only org-level
  access policy tokens are covered: self-hosted admin passwords and stack data-source credentials
  are not. `docs/provider-rotation-matrix.md` had the wrong endpoints
  (`/orgs/{slug}/access-policies/{id}/tokens`); it now lists the documented ones
  (`/api/v1/tokens?region=`).
- **Credential liveness checks and a parity primitive (PANT-1152, GH #141).** `portunus liveness
  check <name>|--all` probes a stored credential against its provider's own API and reports
  `live` / `dead` / `unknown`; `portunus liveness status --failing` lists dead keys oldest first.
  The value only reaches the outbound request header and the response body is never read.
  Results are audited and kept in `liveness-status.json` until a live check clears them. MCP:
  `portunus_liveness_check`, `portunus_liveness_status`. `parity_check()` compares one value
  across targets (GCP, Convex, ...) without exposing a value. Provider probes come with the
  Vercel, Autumn, Clerk and Convex sub-issues. See `docs/liveness.md`.
- **GitHub rotation is now decided per token type (PANT-1141, #188).** `GitHubRotationAdapter`
  is no longer a stub. It resets OAuth App tokens (`gho_`) issued by an OAuth App you own, using
  `PATCH /applications/{client_id}/token`. Because GitHub kills the old token immediately, this
  requires `--retire-old`. Classic and fine-grained PATs, `gh auth login` tokens and App private
  keys are `manual`, since GitHub has no reissue API for them. App user and installation tokens
  are `not applicable`. Every refusal explains why and makes no API call.
- **GitHub liveness check (PANT-1141).** `github` is registered with the shared liveness
  framework (PANT-1152), so `portunus liveness check` now covers GitHub references using
  `GET /rate_limit` (it doesn't count against the rate limit): 200 means live, 401 means dead,
  anything else is unknown. GitHub App private keys aren't bearer tokens, so they report unknown
  without any API call.
- **Resend API key rotation (PANT-1140).** New `ResendRotationAdapter`, registered as `resend`,
  runs via `portunus rotation run <ref> [--retire-old]`. It creates a new key with an admin key,
  checks it with a read-only `GET /api-keys` (no test email; a `sending_access` key passes on 401
  `restricted_api_key`), stores it, and writes the new key id to `tags["resend_api_key_id"]`.
  `--retire-old` deletes the old key, because Resend has no disable. Without it, the old id is
  listed under the `resend` binding's `superseded_key_ids`. `portunus rotation run` now also
  prints `key_id=` when the adapter reports one.
- **YouTrack token rotation (PANT-1138, #191).** `YouTrackRotationAdapter` (`provider=youtrack`)
  rotates a JetBrains Hub permanent token through the Hub REST API. It creates a new token with the
  current token's scope, checks that the new token authenticates as the same user, stores it, and
  deletes the old token only with `--retire-old`. The Hub URL comes from a `hub_url`, `youtrack_url`
  or `youtrack_url_ref` tag. Nothing is hardcoded. The current token's id lives in `hub_token_id`.
  The adapter updates it after each rotation, and lists the old id under `superseded_key_ids` until
  it is retired, the same as Resend. `docs/provider-rotation-matrix.md` had
  the wrong endpoint (`users/me/tokens`). It now shows the documented `users/{id}/permanenttokens`.

### Fixed

- `portunus bindings set` no longer wipes a binding's `impersonate_service_account` when it
  updates another field, and `bindings show` now displays it.

- **OAuth refresh now sends `Accept: application/json`.** GitHub's token endpoint returns
  form-urlencoded unless asked for JSON, so refreshing a GitHub App user token failed to parse.
- **Sentry tokens are `manual`, not `auto` (PANT-1139, #190).** `provider-rotation-matrix.md`
  listed Sentry user auth tokens as auto-rotatable via `POST /api/0/api-tokens/` with an
  `org:admin` bearer token. That call is always rejected: Sentry's user, org and
  internal-integration token endpoints use `SessionNoAuthTokenAuthentication`, which refuses any
  `Authorization` header, and they have done so since 2023. A new `SentryRotationAdapter`
  (`_ADAPTERS["sentry"]`) reports `manual`, and `portunus rotation run` on a Sentry reference
  now prints the human re-issue steps rather than "no adapter registered". The matrix and
  `docs/rotation.md` are corrected.

- **A file-target inject can now be read by a caller running as another uid (PANT-1142).**
  `inject`/`ask` take `--mode 0600|0640|0660` and `--group <name|gid>`, and `POST /api/inject`
  takes `mode`/`group`. Both are applied before the value is written; if Portunus can't apply
  them, the inject fails and leaves no file. Before, every file was `0600` as Portunus's own uid
  and a non-root caller got `ok: true` for a file it could neither read nor delete.
  `POST /api/inject` now also returns `file: {path, uid, gid, mode, readable_by}`, so `ok` can be
  checked against the file actually written. Default stays `0600`.
- **`inject --tags scope=...` matches an open `scope` tag when the structured `scope` is empty
  (PANT-1142).** The same fallback applies to every structured key. `flayr-mdostal-account`
  carries `scope=account-wide` only in its open tags, so `ask` showed the tag but `inject` with it
  found no reference.
- **`GET /api/health` no longer reports healthy while every write is rejected (PANT-1143,
  #186).** `ui/proxy.ts` 403s mutating requests whose `Host` isn't a loopback name unless
  `PORTUNUS_UI_HOST` is set, but GET/HEAD skip that guard, so a UI reached container-to-container
  by service name stayed green with every write failing. The deep health check now runs the same
  Host check on its own request and adds a `ui-write-host` check; when it fails the result is
  `down` (HTTP 503) and the detail says to set `PORTUNUS_UI_HOST`. `?shallow=1` is unchanged.

## [0.34.0] - 2026-10-14

### Fixed

- **File-target inject no longer leaves files behind when the caller gives up (PANT-1003).**
  `POST /api/inject` now kills the `portunus` CLI when the client disconnects and removes a
  file that request created. Before, a caller that timed out deleted its file before the CLI
  wrote it, and the file then stayed on disk. `FileAdapter` also creates the file `0600` from
  the start instead of writing it and then chmod-ing it.

### Changed

- **`requires-python` is now `>=3.10` (PANT-848).** The claimed `>=3.9` was never installable:
  the `mcp` dependency itself requires Python 3.10+. CI now runs pytest on 3.10 and 3.12.
- CI builds and tests `ui/` on every PR into dev/main: `npm ci`, `tsc --noEmit`, `next build`
  and the Playwright E2E suite (Chromium, all API calls mocked). A failed Python install now
  fails CI instead of being swallowed.

### Added

- **Chrome saved-login backend (`portunus-chrome-credential-broker`), macOS + Linux, read-only.**
  New `ChromeBackend` reads Chrome's own saved logins through Portunus's existing resolver
  boundary — useful for test scenarios that need real, already-authenticated browser
  credentials without a separate secret-management step. `sm_name` scheme:
  `chrome:<profile>:<origin>:<username>`. macOS derives the AES key from the login Keychain's
  `Chrome Safe Storage` item (PBKDF2-HMAC-SHA1); Linux tries Secret Service then KWallet,
  surfacing an audit warning (never a silent decrypt) when neither is available and Chrome
  stored the password unprotected. New CLI: `portunus browser profiles` and `portunus browser
  list --profile <name>` — both metadata-only, structurally incapable of returning a password.
  Windows is explicitly deferred (#181); write-back/rotate is explicit backlog (#182). See
  `docs/backends.md`.
- **`portunus health [--json]` deep self-check (PANT-854).** Read-only: checks that
  `PORTUNUS_HOME` exists with 0700/0600 permissions, the registry parses, the audit chain
  verifies, the audit clock matches the log's last `seq` (catching a truncated tail the chain
  can't see), and each backend in use is reachable without touching a value (local vault +
  master key present; gcloud on `PATH` plus a `versions describe latest` probe on one non-WIF
  reference). Prints `{status: ok|degraded|down, checks: [{name, ok, detail}]}` and exits
  0/1/2. Never resolves a value, never writes to the home or the audit log.
- The UI's `GET /api/health` now returns that result (503 when down); `?shallow=1` keeps the
  old CLI-free liveness answer, which the desktop sidecar's readiness poll now uses.
- The MCP `portunus_health` tool runs the same check (`shallow=True` for liveness only).
- The Docker image gains `HEALTHCHECK CMD portunus health --json`, and creates its
  `PORTUNUS_HOME` volume 0700 so a fresh container reports healthy.
- `python -m portunus` now runs the CLI (new `portunus/__main__.py`).

### Fixed

- **UI API rejects cross-origin and non-JSON writes, and binds to loopback (PANT-847).** The new
  `ui/proxy.ts` (Next.js 16's name for middleware) covers `/api/*`. A non-GET request gets 403
  if its `Origin` isn't the UI's own or its `Host` isn't a loopback name (DNS rebinding), and
  415 if it isn't `application/json`, before any route reaches the `portunus` CLI. Before this,
  a `text/plain` POST from any open web page, which needs no CORS preflight, could reach
  mutating routes such as `/api/inject`. `npm run dev`, `npm start` and the README's standalone
  command now listen on `127.0.0.1` by default, and only `PORTUNUS_UI_HOST` changes that.
  Previously only the desktop sidecar pinned it. New `npm run test:bind` checks each command's
  real listening address, and `e2e/api-guard.spec.ts` covers the guard against a stub CLI.
- **Hermetic test suite (PANT-849).** `pytest` now passes with no `gcloud`, `gh` or `portunus`
  on `PATH` and an empty `HOME`. Tests that stub gcloud request one shared `gcloud_on_path`
  fixture for the `which("gcloud")` guard, subprocess tests run `sys.executable -m portunus`,
  an autouse fixture isolates `PORTUNUS_HOME` for every test so none can touch `~/.portunus`,
  and the leak-scan visibility test no longer calls the real `gh`.

### Fixed

- **One release version everywhere (PANT-850).** `__version__` (what `portunus --version` and
  the self-update check report), `manifest.json` and the Tauri desktop shell (`Cargo.toml`,
  `Cargo.lock`, `tauri.conf.json`) said 0.32.0 while `pyproject.toml` and this file said
  0.33.0, so 0.33.0 installs reported 0.32.0 and compared the wrong version against the latest
  release. All now say 0.33.0, and `tests/test_version_consistency.py` fails if any of them, or
  `portunus --version`, drifts from `pyproject.toml`.
- `manifest.json`'s `engine.install` was `pipx install portunus`, which would install an
  unrelated PyPI project. It's now the README's `install.sh` one-liner, and the same test
  checks that it still appears in the README.
- **Audit log reliability (PANT-851).** A missing or corrupt `.clock` no longer restarts
  `seq` at 0 (duplicating sequence numbers): the next `seq` is recovered from the highest
  `seq` in `audit.log` and `.clock` is rewritten. The broker's approval clock uses the same
  recovery, so deleting `.clock` no longer changes whether an approval is still valid.
  `AuditChain.verify()` / `verify_entries()` return `False` on a corrupt or malformed line
  instead of raising, `AuditChain.check()` reports the first bad line and why, and
  `portunus verify` prints `BROKEN at line L: <reason>` and exits 2. `entries()` skips
  unparseable lines instead of raising.

### Fixed

- **Concurrent writers can no longer lose rotation bindings or corrupt state files on crash
  (PANT-852).** `rotation-bindings set` and `vault access import` now do their
  read-modify-write under a new `rotation-bindings.lock` (`update_rotation_bindings()`), so
  CLI, MCP and UI writers running at once no longer drop each other's providers.
- Every JSON state file (registry, local vault, vault/rotation bindings, views, roles,
  leak-scan state, sync state, update cache) is now written through one `atomic_write()`
  helper: a unique temp file, fsynced before `os.replace()`, then a directory fsync after it.
  A crash can no longer leave an empty registry or vault.
- `Registry`, `AuditChain` and `LocalEncryptedBackend` now lock through the shared
  `filelock.flock_path`. The three hand-copied flock loops are gone. Error types are
  unchanged: `RegistryLocked`, and `TimeoutError` (via its `LockTimeout` subclass).

## [0.33.0] - 2026-09-14

### Added

- **OAuth refresh rotation adapter + general rotation framework (PANT-161 / portunus-key-rotation-framework).**
  `OAuthRefreshRotationAdapter` wraps the existing `OAuthBackend`/`OAuthRefreshTokenAuth`
  path — no second implementation of the refresh grant, no drift. One adapter drives every
  stored OAuth credential through a single `run_periodic_oauth_refresh()` job;
  adding a new refresh-token credential requires no new per-provider launchd job (supersedes
  PANT-146/147/149's per-credential approach). Provider-rotated refresh tokens are persisted
  immediately via `store_oauth_credential` (confirmed live against Codex CLI's subscription
  login through `auth.openai.com/oauth/token`). `--retire-old` is rejected with a clear error
  on OAuth refresh rotation — the provider replaces the spent token as part of the grant; there
  is no superseded credential to retire. `RotationResult` is a frozen dataclass structurally
  incapable of carrying credential material: key identifiers, phase, booleans, and timestamps only.
  `rotation_adapter_for("oauth")` resolves the new adapter.

- **`OAuthRefreshTokenAuth`: optional `client_secret` + JSON request body support.**
  `client_secret` is now `Optional[str]` (a genuinely public/PKCE client has none to supply;
  sending an explicit empty secret is rejected by some token endpoints). New `request_format`
  param (`"form"` default, unchanged; `"json"` for Codex-shaped endpoints) picks the
  Content-Type and wire serialization. `OAuthBackend.access()` reads `client_secret` via
  `credential.get()` and threads `request_format` from the stored bundle.

- **GCP service-account impersonation for vault bindings.**
  `VaultBinding.impersonate_service_account` lets a stable, non-reauth-walled identity
  (e.g. a personal Google account) mint short-lived tokens for a project-scoped service account
  via `--impersonate-service-account=` alongside `--account=`. Requires
  `roles/iam.serviceAccountTokenCreator` on the target. Persisted in vault-bindings.json,
  backward-compatible with existing bundles.

- **`docs/rotation.md`** — the single human-facing answer to "what rotates, what doesn't, and
  what do I have to do by hand." Covers the non-destructive default and its rationale, the
  provider capability matrix (auto / manual / stub), the OAuth refresh flow, and an explicit
  section for `manual` providers (Linear personal API keys, GCP service-account keys) stating
  that no auto-rotation is possible and human re-issue is permanently required.

### Fixed

- Corrected stale claims in `rotation.py`'s module docstring ("No real provider API is ever
  called from this module") and `mcp_server.py`'s `portunus_rotation_status` docstring
  ("Every provider is a stub today -- no real rotation has ever fired") -- both were false
  once `OAuthRefreshRotationAdapter` landed.

## [0.32.0] - 2026-09-02

### Added

- **Session-access gate + a corrected Playwright bridge (portunus-session-access-gate).**
  `Broker.check_session_access()` gates `session load` through the exact `roles.evaluate()` seam
  `check_injectable()` already uses — a synthetic, in-memory `Reference` (never persisted),
  confirmed the policy engine is fully duck-typed and needs no new gating mechanism. `session
  store` gains optional `--org`/`--project`/`--env`/`--repo` scope metadata (additive, backward
  compatible with existing sessions); only `session load` is gated, matching `check_injectable`'s
  own fetch-only gating precedent. Re-scopes `portunus-session-vault`'s own Stories 03/05
  (confirmed unstarted, surfaced by a branch-cleanup audit — GitHub issue #136) against current
  code rather than resurrecting two years-stale branches as-is. Live-verified against a real
  Chromium browser context (not a synthetic fixture) — the proof caught and corrected a real
  documentation error along the way: `session load`'s tempfile holds the full record, not the
  bare Playwright `storageState` payload; a one-line unwrap bridges it, no new production module
  needed. See `docs/architecture.md` §21 for the full record, including the correction.

## [0.31.0] - 2026-09-02

### Added

- **OAuth credential entry dashboard (portunus-oauth-token-broker Story 05).** A new "+ Add
  OAuth credential" button in the Standalone UI opens `OAuthCredentialForm` — the same
  metadata-rich field set `AddSecretForm` already gives regular secrets, with an explicit,
  never-ambiguous choice between two credential sources: auto-fill from a detected local
  `gcloud` ADC file (read server-side only — the values are never sent to or shown in the
  browser, only a `{available: true/false}` presence check crosses that boundary) or manual
  entry (masked `client_secret`/`refresh_token` fields). Submitting chains the same two CLI
  calls a human would otherwise run by hand (`portunus oauth store`, then `portunus drop
  --backend oauth`) — no new storage/minting mechanism, no behavioral fork from a CLI-entered
  credential. Live-verified end to end: a form-submitted, auto-filled credential resolved to a
  real access token at the boundary.

## [0.30.0] - 2026-09-02

### Added

- **Settings: vault bindings visibility (fixes #128).** `/api/bindings`' GET route now supports
  listing every configured binding (`project` param is optional — the CLI's own `portunus
  bindings show` already supported this, the route just never exposed it). New "Vault bindings"
  section in Settings shows every project's backend/sync_mode/account at a glance — real, live
  data, nothing invented or presence-only. Project Explorer remains where a binding is actually
  edited (unchanged, prior deliberate scope call); this is read-only visibility/audit, which
  nothing in the UI gave an operator before this.

## [0.29.0] - 2026-09-02

### Added

- **Generic OAuth 2.0 token broker (portunus-oauth-token-broker).** The legitimate answer to
  "capture a Gmail session": Portunus is storage + minting only, never the consent flow itself
  (that stays a one-time, provider-legitimate bootstrap outside Portunus — e.g. `gcloud auth
  application-default login --scopes=...`, the same delegation precedent `portunus auth login`
  already established). New `portunus oauth store/list/remove` stores a refresh-token credential
  bundle encrypted at rest (mirrors the existing session vault's exact plumbing, under a distinct
  `oauth:` namespace, deliberately TTL-free since the provider itself is the real authority on
  validity). New `--backend oauth` mints short-lived access tokens on demand via the standard
  OAuth 2.0 refresh_token grant (`OAuthRefreshTokenAuth`, mirroring `GCPWorkloadIdentityAuth`'s
  exact shape but generic across providers), cached in-memory until near expiry. Zero
  Resolver-side changes — an oauth reference flows through the exact same
  resolve/resolve_exec/resolve_to_tempfile boundary paths every other secret already uses.
  Multiple accounts per provider is first-class: `(provider, account)` namespacing, live-verified
  with two real, distinct Google accounts minting genuinely different access tokens. See
  `docs/architecture.md` §20 for the full research/design trail, an SSL-cert environment gap
  found and documented (not a Portunus bug), and a real mid-proof mistake (a boolean-comparison
  bug briefly printed a live short-lived access token to this session's own output) disclosed and
  fixed rather than glossed over.

## [0.28.0] - 2026-09-01

### Added

- **Masked, trimmed, agent-openable secret entry (portunus-secure-entry).** `cmd_drop`'s
  `--stdin`/`--value-file` read now `.strip()`s surrounding whitespace instead of only the
  trailing newline — a copy-pasted leading/trailing space or `\r` previously corrupted the
  stored value silently; the web dashboard's `/api/drop` route inherits the fix for free.
  Omitting both `--stdin` and `--value-file` now prompts interactively via `getpass` (masked,
  entered twice, refusing on a mismatch) — for a human at their own terminal, since an agent's
  own tool-execution channel has no TTY and can't itself use this mode (verified directly:
  `getpass()` against closed/empty stdin raises `EOFError` immediately, caught and reported
  cleanly rather than as a raw traceback). The already-real `AddSecretForm` dashboard now has a
  one-click "Fulfill…" action on any `state=requested` reference (from `portunus ask "add
  ..."`), pre-filled from every metadata field the agent already captured, plus a
  `?fulfill=<name>` deep link. New **`portunus ui open [--fulfill NAME]`** is the one piece an
  agent's own non-interactive tool call can actually run itself — fire-and-forget, opens a
  browser tab pointed at that pre-filled form, refusing clearly before ever opening a
  misleading or unreachable URL. Live-verified end to end via Playwright and a real dev server —
  see `docs/architecture.md` §19.

## [0.27.0] - 2026-08-29

### Added

- **`portunus vault access export/import/verify`** — scoped, plain-JSON sharing of *working
  access* to a vault between two Portunus instances, distinct from `vault export`/`import`
  (which moves the whole vault including every value). A bundle carries registry pointers +
  bindings only — it structurally cannot contain a secret value, so it's never
  passphrase-locked. Each exported reference carries a `resolved_backend` field, computed once
  on the exporting instance (mirroring `_make_backend_router`'s own precedence) and never
  re-derived on import, since only the exporter knows its own global backend default. A
  `resolved_backend="local"` reference always lands as `state=requested` on import regardless
  of its source state — the value lives only on the source machine — while every other
  backend's pointer transfers unchanged, since the value lives with the cloud provider, not
  locally. Conflicting entries (same name, different `sm_name`/backend) are refused without
  `--force` and reported clearly, without aborting the rest of the batch. `verify` makes a real
  per-reference reachability check through the same boundary-safe `resolve_call()` a real
  `resolve`/`ask`/`mcp` call uses, translating failures into actionable `portunus drop`/`gcloud`/
  `portunus auth login` hints instead of a raw traceback; CLI-only, no MCP tool, since it can
  trigger real backend API calls across the whole registry in one invocation. Live-verified
  against a real, scoped export of the actual vault, imported and verified against a throwaway
  `--home` — see `docs/architecture.md` §18 for the full live-proof record, including a real
  finding about an unused `master.key` artifact this epic's own proof surfaced (pre-existing,
  unrelated to this feature, left as a known gap).

## [0.26.0] - 2026-08-19

### Added

- **Per-agent access control (Petitio), real and opt-in (portunus-petitio-rbac).**
  `PolicyRecord` gains `principal` (scope a policy to one agent identity; blank/`*` = everyone,
  fully backward-compatible with every existing `roles.json` record) and `"repo"` joins
  `org`/`project`/`env` as a valid scope type. New `roles.evaluate(policies, requester, ref) ->
  Decision` is the single seam all scope/precedence logic lives in (flat-OR for v1, a deliberate
  choice — every call site only ever calls this function and acts on its result, never
  reimplements matching itself, so a future precedence model is a one-function change, not a
  rewrite). Every resolve with a real requester now gets a `would-allow`/`would-deny` audit
  line. New `portunus roles enforce on|off|status` (default: off) is the explicit opt-in that
  makes a `would-deny` decision actually raise `NotAuthorized` — permissive-if-unconfigured
  holds even with enforcement on: a scope with zero configured policies always stays open. A
  brand-new `PORTUNUS_HOME` defaults to enforcement on; an existing vault's setting is never
  retroactively changed. `--principal` on `portunus roles set/delete`; Settings' roles form
  gains a principal field, `repo` as a scope option, and updated audit-only labeling. Live-
  verified against the real vault (410 references) — a matching principal allowed, a
  non-matching one denied, and every reference outside the one configured scope unaffected. See
  `docs/architecture.md` §3/§17 and `.pHive/research/petitio-rbac-synthesis.md` (the 13-agent,
  multi-provider research this epic is grounded in).
- `portunus agent init`/`portunus agent status` — a single command that wires Portunus into
  whatever AI coding agent CLIs are already on the machine (Claude Code, Codex CLI today):
  registers the MCP server for each detected harness and installs the four usage skills
  (`portunus-ask`/`portunus-drop`/`portunus-vault-setup`/`portunus-vault-audit`, previously only
  set up by hand, only in this repo) to `~/.claude/skills/` so they apply everywhere, not just
  when working inside this codebase. Idempotent — safe to re-run any time, e.g. after installing
  a new harness. `--harness claude`/`--harness codex` (repeatable) narrows `init` to one harness.
- `curl -fsSL https://mdostal.github.io/portunus/install.sh | bash` — the new top-line onboarding
  path: installs the CLI from GitHub, then runs `portunus agent init`.
- **`portunus update check`/`portunus update run`** — CLI self-update. `check` is a live,
  read-only lookup against GitHub releases (via the user's own `gh` CLI, same posture as the
  desktop app — never a token this tool holds itself). `run` always re-checks live, refuses on a
  dev/editable install (run `git pull` instead), and requires an interactive confirm or `--yes`
  before installing — never a silent unattended swap. Installs are pinned to the exact resolved
  release tag (`git+...@vX.Y.Z`), never a floating `main` HEAD. A lightweight passive check also
  runs once per CLI invocation (throttled to once per 24h, fully detached/non-blocking, disabled
  during tests and via `PORTUNUS_NO_UPDATE_CHECK=1`) and prints at most one line to stderr when a
  previously-cached check found something newer — it can never install anything itself, only
  `update run` can, verified structurally. Given this tool holds real vault access, the update
  path was deliberately built with zero legitimate reason to import any vault machinery at all.
- `SECURITY.md` (private vulnerability reporting via GitHub Security Advisories) and
  `CODE_OF_CONDUCT.md` (Contributor Covenant v2.1) — `CONTRIBUTING.md` referenced a code of
  conduct that didn't actually exist. `pyproject.toml` gains PyPI classifiers and `Repository`/
  `Issues`/`Changelog` project URLs. `.github/PULL_REQUEST_TEMPLATE.md` added.

### Fixed

- `NotAuthorized` (the new exception above) wasn't caught anywhere `NotInjectable`/
  `ApprovalRequired` already were: `cmd_resolve` would have shown a raw Python traceback instead
  of a clean error, and — worse — `cmd_sync`/`portunus_sync`/`leakscan.get_values()` would have
  crashed their entire loop on the first policy-denied reference instead of skipping it
  gracefully like every other inaccessible reference already does. Fixed at all four sites.
- The desktop app's own auto-updater (`updater.rs`) had a real bug found while building the CLI
  version above: `gh release view --repo <repo> latest ...` treats `latest` as a literal release
  tag to look up, which fails with "release not found" unless a release happens to be tagged
  exactly `latest` — it never was, so the desktop updater's `gh` call had been failing on every
  check since it shipped, silently (the background timer only logs a warning on error). Fixed by
  omitting the tag argument entirely, which is what actually means "show the latest release."

### Changed

- Pre-release scrub: real GCP project IDs (`personalsites-487021`, `ffe-cicd`) and real personal
  email addresses used as worked examples throughout README/CHANGELOG/`.pHive/epics/**` (from
  real dogfooding sessions) replaced with placeholders (`demo-project-483920`, `demo-cicd`,
  `personal@example.com`, `work@example.com`) — 242 substitutions across 56 files, so the public
  history doesn't point at any real live infrastructure. Author name/company/site were left
  untouched (already public). The `.pHive/epics/` paper trail itself stays — only the
  identifying specifics inside it were scrubbed.
- README's "13 tools"/"35-command CLI" summary corrected to the real current counts (21 MCP
  tools, 35 CLI commands as of this release); MCP tool table gains the previously-undocumented
  `portunus_rotation_status`.
- `pyproject.toml`'s package name is now `pantheon-portunus` (the installed command is still
  `portunus`) — PyPI's existing `portunus` is an unrelated, unmaintained package
  ([`IQTLabs/portunus`](https://github.com/IQTLabs/portunus)); the README's prior
  `pipx install portunus # once published` instruction would have silently installed the wrong
  tool. Not yet actually published under either name — the new installer pulls from GitHub in
  the meantime.

## [0.25.2] - 2026-08-16

### Fixed

- Desktop app icon had a fully opaque white background baked into its rounded corners, visible
  as a white border/card artifact in the Dock and anywhere else the icon renders. Fixed by
  establishing a clean master source (`ui/src-tauri/app-icon.png`) with transparent corners and
  regenerating the full macOS/Windows icon set from it via `cargo tauri icon`. The same defect
  in the GitHub Pages landing page's hero image and favicon/apple-touch-icon assets was fixed
  directly on the `gh-pages` branch (not versioned with the app itself).

## [0.25.1] - 2026-08-16

### Fixed

- `portunus reg rm <name>` now also purges the underlying stored value from whichever backend
  actually serves the reference (today: local-encrypted only), not just the registry pointer.
  Previously the value was left orphaned in the vault forever after "removing" a reference.

## [0.25.0] - 2026-08-16

### Added

- **Git-repository history as a leak-scan target.** `portunus leak-scan config add-repo <path>`
  scans a repo's FULL history (every branch, every commit via `git log --all -p`), not just its
  current working tree -- formalizing the manual technique that verified live no leaked
  credential had ever touched this codebase's own history. Always a full re-scan per run (git
  history can be rewritten, unlike an append-only log file); reuses the existing scan engine
  unchanged via a temp dump, no second matching implementation.
- **Source classification on every finding.** `log` / `local` / `git-history`, and for
  git-history findings, whether the repo's GitHub remote is `public`, `private`, or `unknown`
  (resolved via `gh repo view`, the same gh-CLI/your-own-credential posture the desktop app's
  self-updater already uses -- never an embedded token, never a guess). A public-repo finding
  gets the loudest treatment anywhere it renders -- the single most severity-relevant fact this
  feature can surface.
- Settings gains git-repo config (add/remove, mirroring the existing scan-path UI); DetailDrawer's
  finding history now labels each entry by source ("⚠ PUBLIC repo: ...", "private repo: ...",
  "log file", "local file"). `portunus leak status --detail` and the MCP/API equivalents expose
  the full per-finding classification.

## [0.24.0] - 2026-08-16

### Added

- **LeakBadge across the UI.** A `⚠ leak: <severity>` badge now follows a leaked reference
  everywhere its name renders -- Console (plus a new "Leaked" facet), Vault Map, Project
  Explorer, and DetailDrawer (with a full expandable file:line history and a working "Mark
  rotated" action). Hover shows "leaked in N conversations" -- distinct files, not raw finding
  count. Independent from the existing rotation-requested flag -- an agent-requested rotation
  and a leak-detected one are different facts, not the same signal.
- **In-app report view.** Settings gains a "View report" button rendering `portunus report`'s
  Markdown output in-app via a small custom renderer (no new dependency -- `ui/package.json`
  stays at 3 runtime dependencies); "Download report" still saves it as a file.
- `summarize(..., detail=True)` (leakscan.py) exposes per-finding path/line detail and a
  distinct-files count for a named reference -- wired through `portunus leak status --detail`,
  `portunus_leak_status(detail=True)`, and `/api/leak-status?name=`. `detail` defaults to False,
  so no existing caller's output changes.

## [0.23.1] - 2026-08-16

### Added

- **Container image.** New `Dockerfile` packages the CLI + MCP server (not the UI) as a
  non-root image with `PORTUNUS_HOME` declared as a persistent `VOLUME` -- the real install path
  for most deployments: a sidecar container or a Kubernetes pod, not "download the desktop app."
  Targets same-pod/same-host reachability (`docker exec`, a shared pod volume, or Portunus
  starting the consumer via `resolve --exec`) rather than a network-shared broker service, since
  the MCP server is stdio-only today. `gcloud` CLI is included for GCP backend support.
- New `docker-compose.yml` -- a runnable worked example of the sidecar pattern (a `portunus`
  service + an `app` service sharing one named volume).
- README.md gains a "Running in a container" section: the persistent-volume requirement for the
  local-encrypted backend, the two real deployment shapes, a per-backend/per-environment auth
  table (GKE Workload Identity marked as the recommended production path), and a tested Podman
  rootless-UID note.

### Fixed

- Stale package description in `pyproject.toml` (still referenced "the Dostal harness") updated
  to match the project's current standalone-first positioning.

## [0.23.0] - 2026-08-15

### Added

- **Leak detection.** New `portunus leak-scan` detects whether a managed secret's actual value
  shows up somewhere it shouldn't -- logs, `.claude` conversation transcripts, shell history, or
  any other explicitly human-configured local path (never a default; nothing is scanned until a
  path is added via `portunus leak-scan config add-path`). Line-based, incremental scanning
  (per-file watermarks -- a rescan only reads newly-appended bytes) proved against real scale
  data (3.4 GB / 4,421 files found under one user's `~/.claude` alone during planning). A match
  escalates a visible severity (warn -> urgent -> critical) over `portunus leak status` as time
  passes, cleared via `portunus leak mark-rotated <name>` -- a documented human assertion, not a
  verified fact. Advisory only: proven, not asserted, that `check_injectable()`/`resolve()`
  behave identically regardless of leak status, mirroring the `roles.json` stub precedent.
- **Settings "Leak scan" section** -- configured paths, a severity-badged findings table,
  "Run scan now" and "Mark rotated." Explicit copy: this is a detective, not preventive,
  control.
- **Full MCP automation surface for leak-scan.** `portunus_leak_status` (read-only) plus, by
  explicit user decision widening the epic's own initial CLI/UI-only scope,
  `portunus_run_leak_scan`, `portunus_leak_scan_config_show/add_path/remove_path`, and
  `portunus_leak_mark_rotated` -- an MCP-connected agent can now trigger a scan, but only over
  paths a human has already configured; every tool is structurally verified to never surface a
  value or file content.
- New `.claude/skills/portunus-vault-audit` skill wrapping crawl/report/leak-scan for
  convenient session-triggered use, and a documented cron/launchd example for scheduled
  `portunus leak-scan` runs (already CI-ready: exits 1 on new findings, 0 otherwise).

## [0.22.0] - 2026-08-15

### Added

- **Metadata crawl.** New `portunus crawl [--org] [--project] [--json]` and the
  `portunus_crawl_candidates` MCP tool bundle everything already known about a reference
  missing description/purpose/org (sm_name, group, project, org, repo, its project's vault
  binding, its provider's rotation binding) into one context object -- a discovery tool, not a
  writer and not an LLM caller. Read it, then propose fields via the existing
  `portunus_suggest_metadata`. Follow-on to 0.21.0's suggest/confirm workflow, the "bulk-
  suggest" work that epic explicitly deferred.
- **Deploy-docs report.** New `portunus report [--org] [--project] [--out path]` renders
  current vault state as Markdown -- org -> project structure, each reference's known
  metadata, and an explicit Gaps section. Independent of crawl -- useful immediately as a real
  starting point for a company with no deploy documentation, whether or not any crawl-sourced
  metadata has ever been confirmed.
- **Settings "Crawl & report" section** -- lists references still missing metadata (reuses the
  existing completeness derivation), a "Fetch crawl bundle" button framed honestly as context
  for an LLM session to read (never an automatic filler), and a "Download report" button.
  `/api/crawl` and `/api/report` are thin shells over the CLI, matching every other route.

## [0.21.0] - 2026-08-15

### Added

- **LLM-suggests, human-confirms metadata.** New `portunus_suggest_metadata` MCP tool proposes
  description/purpose/tags/group -- lands in a pending sidecar, **never the live field**. A new
  `DetailDrawer` block shows "suggested by \<agent\>: '...' [Confirm] [Reject]" per pending
  field; confirm applies via the exact same `retag()` a manual edit would use, reject discards
  without ever touching the live field. `portunus metadata confirm/reject/pending`. Routing
  fields (org/provider/project/env/repo/backend) are never agent-suggestible.
- **Role/policy schema -- STUB ONLY.** `portunus roles set/delete/show` and a new Settings page
  let you record `{scope: org|project|env, role, actions}` policy records, and they genuinely
  persist -- but nothing enforces them yet. `check_injectable()`/`retag()` are byte-identical
  whether or not any policy exists (proven directly, not just asserted). Deliberate, staged
  groundwork for Petitio's future access-level engine.
- **Settings page** -- org/project hierarchy overview + the roles config above, always visibly
  labeled "not yet enforced."
- **Expanded first-run setup wizard** -- shown automatically the first time the UI opens against
  an uninitialized vault (never again once anything's configured). Explains Portunus's three
  parts, walks through choosing your first vault's backend (including the stub adapters,
  explicitly framed as "your first vault, not your only one"), an in-UI trigger for
  `gcloud auth login` if GCP is chosen, a (literally disabled) preview of the coming-soon roles
  step, and a first `discover` pass.
- **In-app About/Help page** -- what Portunus is, the component model, how to organize a large
  vault, how the suggest/confirm workflow works, best practices, and an honest disclosure that
  roles aren't enforced yet.

This completes the `portunus-vault-trust-and-access` epic (10 of 10 stories) -- the second and
final checkpoint after v0.20.0's metadata-quality/org-hierarchy half.

## [0.20.0] - 2026-08-15

### Added

- **`org` field** — an organizational umbrella one level above `project` (e.g. `firefly-events`
  spanning `demo-cicd`/`shindig`), the same flat-structured-tag pattern `provider`/`project`/
  `env`/`repo` already use. Wired through `reg add`/`retag`/`retag-bulk`/`drop` and into
  `retag()`'s collision check.
- **Sub-vault navigation** — Vault Map is now a real org → project drill-down (each level
  showing its own reference count and completeness summary) instead of one flat card wall —
  the fix for "the map is unmanageable once a vault spans 30+ repos." Built entirely on the
  `org`/`project` fields, no new store.
- **Missing-metadata warning** — a real "⚠ missing metadata" badge wherever a reference
  renders, plus a clickable Metadata facet in Console to filter down to exactly what needs
  filling in. Purely derived from existing fields, nothing new stored.
- **Custom views** (`portunus views create/add/remove/delete/show`) — named, human-curated
  reference collections for task-shaped clustering that doesn't map onto org/project/env
  ("everything for the Shindig deploy"). New `views.json` store, locked from day one (every
  mutator wraps its own load-mutate-save in one `flock` acquisition — proven with a real
  10-process concurrent-write test, zero lost entries).

### Fixed

- **`repo`/`source_files` never reached the UI** despite working end-to-end in the CLI/
  Registry already — `/api/retag` and `/api/drop` now forward both fields, and both edit forms
  (Add Secret, DetailDrawer's Move) gained the missing inputs. `portunus drop` itself was also
  missing `--source-files` entirely (only `--repo` existed) — added for symmetry with `retag`.

This is a checkpoint release — the first 4 of 10 planned stories in the
`portunus-vault-trust-and-access` epic (`.pHive/epics/portunus-vault-trust-and-access/`), the
part directly answering "get the metadata right and stop the vault feeling like one giant flat
list." Stubbed role/policy schema, an LLM-suggests/human-confirms metadata workflow, a Settings
page, an expanded setup wizard, and an in-app About/Help page are planned as a follow-on
release.

## [0.19.0] - 2026-08-14

### Added

- **Vault-binding `account`/`wif_audience` editable in Project Explorer.** Backend/sync-mode
  were already live in the UI's binding panel; `account` (a local gcloud identity) and
  `wif_audience` (the WIF provider resource name) are now too, via a new `POST /api/bindings`
  write path. Neither is a credential — account is an identity *selector* (the identity itself
  must already be authenticated locally), wif_audience is infrastructure topology — so neither
  is masked in the UI, matching the CLI flags they mirror.
- **Rotation-binding `account` hint editable in a reference's detail view**, next to the
  Auto-rotate button — a new `POST /api/rotation-status` (was GET-only). `status` (`stub`/`real`)
  stays derived from the real adapter registry and is structurally unreachable from the UI: the
  handler never reads a `status` field off the request body at all, so a stub provider can never
  be made to look real through the browser.

### Fixed

- **A stale, factually-wrong comment** in `ui/app/api/bindings/route.ts` claiming the WIF
  audience value is "never returned by `bindings show`" — verified false by direct CLI test
  (`bindings show --json` already returns the real value). Corrected while adding the new write
  path for the same field.

## [0.18.0] - 2026-08-14

### Added

- **`discover --register` eagerly warms the local cache** (CLI + MCP) for a project bound
  `sync_mode=cached` — collapses "add a key to GCP Secret Manager" from a multi-step dance
  (register, then wait on the first real resolve to trigger the cache pull) down to one step.
  The reference's `state` still lands at `requested`: it's cached sooner, not injectable sooner
  — `check_injectable` still gates every real resolve/inject/ask/MCP path exactly as before.
  Per-reference, best-effort: a cache-warm failure never fails registration itself.
- **`portunus vault export`/`portunus vault import`** — a coordinated, passphrase-locked
  portable snapshot of the vault's critical-state surface (reference registry, master key,
  encrypted values, vault bindings, legacy bindings if present, audit log + its sequence
  counter). Re-encrypted under an operator passphrase (PBKDF2-SHA256, 600k iterations) — the
  archive never carries the vault's own live decryption key. `import` fails closed on a wrong
  passphrase and refuses to touch an existing non-empty vault without `--force` (a full
  replace, never a merge). CLI-only: no MCP tool, no UI surface — an archive containing every
  secret in the vault should never be triggerable by an LLM-facing tool without a human
  directly running the command.

### Fixed

- **`vault-bindings.json` had no lock at all** — the only critical-state file without one,
  unlike the registry/audit-log/local-vault (each already fixed for a real concurrency bug
  earlier this cycle). `save_vault_bindings()` now serializes its write through a dedicated
  flock, and the new coordinated multi-file snapshot primitive backing `vault export` acquires
  every relevant lock (registry, vault, vault-bindings, audit) in one fixed order before
  reading, so an export is never caught mid-mutation.
- **Audit chain restore hazard, caught by the export/import round-trip tests themselves**:
  `.clock` (the append() sequence counter) wasn't included in what a snapshot captured —
  restoring `audit.log` without it would silently reset the counter, letting the next real
  append re-mint a `seq` that already exists in the restored chain and break the hash-chain
  invariant `verify()` checks. `.clock` now travels with `audit.log` under the same lock.

## [0.17.0] - 2026-08-15

### Added

- **`repo` + `source_files` reference metadata.** Found by inspecting the real data first: all
  342 `demo-cicd` references share one GCP project across many repos/services, with nothing
  distinguishing *which git repo* actually consumes a secret. `repo` is a new structured field
  (`find --tags repo=...` works immediately, same as provider/project/env); `source_files` is a
  list of file paths in that repo, same optional posture as `related`.
- **`portunus retag-bulk --group-prefix <p> [--repo ...] [--source-files ...] [--dry-run]`** —
  backfills every reference whose `group` starts with a prefix in one command instead of one
  `retag` per reference. `--dry-run` previews with zero writes; one reference's collision
  failure never aborts the rest of the batch (same precedent `drop-bulk` set).
- **`portunus tree --by {group,repo}`** (CLI + MCP) — the same tree render, keyed on the
  structured `repo` field instead of the free-text `group` path. A reference with no `repo`
  lands under a `(no repo set)` bucket, same non-dropping guarantee `(ungrouped)` already has.
  Project Explorer's UI tab gets a matching Group/Repo toggle.
- **`related` renders as clickable chips** in the UI's detail view instead of plain text —
  clicking one switches straight to that reference. A relationship-graph *visualization* was
  explicitly considered and deferred: `related` has 2 real data points in the whole vault today,
  not yet a substrate worth a graph renderer.

Live-verified against the real `demo-cicd` data (384 references in the vault at the time): `retag-bulk
--group-prefix demo-cicd/event-api --repo event-api --dry-run` correctly identified 91
references with zero collisions and made zero writes. The actual backfill against real
production data is an explicit follow-up for whoever owns that vault to confirm, not something
this release does unsupervised — the real data's repo naming is a judgment call, not something
to guess at automatically.

## [0.16.3] - 2026-08-15

### Fixed

- **Local vault: concurrent `store()`/`remove()` could silently lose writes.** Found live from
  a real bug report — a different session resolving a shared, `sync_mode=cached` reference saw
  Portunus serve a stale value while other sessions saw it working fine. Verified directly
  first (hashed Portunus's served value against a fresh direct GCP fetch — identical, ruling
  out an ongoing bad value), which pointed at a timing-dependent race rather than a static bug.
  `LocalEncryptedBackend.store()`'s load-mutate-flush was unlocked — the same class of bug just
  fixed in the audit chain (0.16.2), but here governing actual secret values in a codepath
  (`SyncingBackend`'s cache refresh) multiple concurrent sessions hit by design. Reproduced
  directly: 20 concurrent `store()` calls lost 14 of 20 writes and crashed twice on a shared
  temp filename. Fixed with the same `flock` idiom `Registry`/`AuditChain` already use.

## [0.16.2] - 2026-08-15

### Fixed

- **Audit chain: a concurrency race could break the hash chain.** Found on the real
  production vault — `portunus verify` reported BROKEN. `AuditChain.append()`'s
  sequence-counter increment and its read of the prior entry's hash were both unlocked; two
  processes appending close together (two `portunus resolve` calls, or a race against the MCP
  server handling a concurrent request) could read the same "last" state before either wrote,
  producing a duplicate `seq` and an unrecoverable break. Fixed with the same `flock` idiom
  `Registry` already uses. Does not retroactively repair an already-broken historical chain —
  see the fix's own notes for why that's a separate, explicit decision, not something to do
  silently.

## [0.16.1] - 2026-08-14

### Fixed

- **Desktop app: sidecar-spawned `portunus` CLI calls could hang indefinitely.** Found by
  actually installing and running the app against the real vault instead of only scratch
  vaults. The spawned CLI subprocess's working directory defaulted to wherever the sidecar
  process happened to be running from — inside the installed app, that's the bundled Next.js
  build's own `node_modules` tree, and Python's import system treats the working directory as
  an implicit module search path, so a filesystem scan over that large bundled tree could hang.
  Fixed by pinning the subprocess's working directory explicitly instead of inheriting it.

## [0.16.0] - 2026-08-14

### Added

- **Tauri desktop app (macOS).** A native menu-bar shell around the existing Next.js Vault UI
  (`ui/src-tauri/`) — no more `cd ui && npm run dev` every time; `npm run dev` stays fully
  valid alongside it. Wraps the UI's existing `output: "standalone"` build as a sidecar,
  capturing the real login-shell PATH before spawning it (GUI-launched processes get a
  near-empty one, confirmed live, not assumed) and always picking a fresh free port (never a
  hardcoded one). Tray icon: Open Vault / Check for Updates… / Launch at Login / Quit —
  closing the window hides it (sidecar keeps running); Quit explicitly kills the sidecar,
  verified against the actual Apple Event quit path (Cmd+Q/Dock), not just a raw signal.
  Single-instance enforced — a second launch focuses the existing window.
- **Self-update, without an embedded credential.** Checks this (private) repo's latest GitHub
  release via the user's own already-authenticated `gh` CLI — never a token baked into the
  shipped app, the exact credential-in-a-binary anti-pattern this project exists to prevent.
  Always asks before installing (a native dialog, never a silent unattended swap). The actual
  swap is a detached relauncher script with a required, tested failure path: verifies the new
  bundle (Info.plist + codesign) before touching anything, backs up the original during the
  swap, and restores it on any failure — verified live with a deliberately corrupted download
  (rejected cleanly, original untouched) and a simulated mid-swap failure (original correctly
  restored from backup).
- Ad-hoc signed (macOS Apple Silicon) — a deliberate v1 scope decision for a single-user,
  single-machine tool, not a gap; full Apple notarization is a separable follow-up only needed
  for public distribution. `.github/workflows/release-desktop.yml` builds and attaches the
  `.app` to the same `vX.Y.Z` release this project's Python package already ships under — one
  version number, one release, no second parallel versioning scheme.

## [0.15.0] - 2026-08-14

### Added

- **Unified metadata form.** `AddSecretForm` (the UI's create-time form) now exposes
  `kind`/`scope`/`backend` plus `description`/`purpose`/`injected_as`/`group`/`related` —
  the full field set `DetailDrawer`'s edit-time Move form already had, all wired through the
  existing `/api/drop` → `portunus drop` path. No new backend concepts; every field but one
  already had full CLI/route support.
- **`portunus drop --backend`.** Closes a confirmed, small gap: `portunus_drop`'s MCP tool and
  `drop-bulk`'s per-entry `backend` already let a caller override which backend a single
  reference uses; the CLI's one-off `drop` command didn't. Now it does, matching
  `Reference.backend`/`Registry.add`'s existing kwarg exactly.
- **Rotation provenance.** New `rotation.py`: `RotationBinding` (provider/status/account),
  keyed by provider like `VaultBinding` is keyed by project, persisted at
  `PORTUNUS_HOME/rotation-bindings.json`. Three stub `RotationAdapter`s (Vercel — the
  confirmed priority target for the first real one — GitHub, Stripe), each `.rotate()`
  unconditionally raising, matching every ARCA stub backend's own restraint. CLI
  `rotation-bindings set/show`, MCP `portunus_rotation_status` — metadata only, never a
  credential. `DetailDrawer`'s "Auto-rotate…" button (inert since v0.14.0) is now wired to
  this registry's derived real/stub state instead of a hardcoded `disabled` attribute —
  behaviorally unchanged today (every provider is still a stub) but structurally real, and
  verified live in-browser to flip both directions when a binding's status changes.
- **`docs/architecture.md` §5** — rotation provenance, including the recursive design decision
  that a future real adapter would authenticate using its own admin credential, itself just
  another Portunus-managed `Reference` resolved through the same boundary-only
  `resolver.resolve_call()` every other value uses. Aspirational — zero real adapters exist yet.

## [0.14.0] - 2026-08-14

### Added

- **Five honest ARCA stubs** — HashiCorp Vault/OpenBao, Infisical, Doppler, 1Password Secrets
  Automation, Azure Key Vault — each interface-conformant and fails closed with a clear error
  and a link to `.github/ISSUE_TEMPLATE/adapter-request.yaml`, matching
  `AWSSecretsManagerBackend`'s existing restraint. Local + GCP remain the only real backends,
  per a 10-agent research workflow where all six candidates independently recommended
  stub-only (no validated environment to build a real adapter against yet).
- **Offline-resilient sync-down.** `SyncingBackend` now falls back to the last-known-good local
  copy (`last_sync_result="stale-offline"`) when a real connectivity failure interrupts its
  recency check, instead of hard-failing — without ever marking that copy as verified-fresh.
  This is what actually makes a `sync_mode="cached"` project keep working while disconnected.
- **Petitio's first seam.** `Identity` (name + kind: human/agent/system) and an optional,
  deliberately inert `requester` parameter on `Broker.check_injectable` — every caller is
  currently allowed regardless of `requester`. Real role-based enforcement (a policy store, an
  escalation-request flow modeled on Teleport's request→review→time-boxed-grant pattern,
  researched and documented) is designed but intentionally not built this release.
- **UI: two-zone backend picker.** Real backends (Local, GCP) and honest stubs never share a
  click target — selecting a stub opens an explanatory modal and a pre-filled GitHub
  adapter-request link instead of a config flow, per OSS adapter-marketplace research (Grafana/
  Airbyte/dbt/Terraform Registry precedent).
- **`docs/architecture.md`** — this repo's first adopter-facing architecture reference, with
  four diagrams: the component model, ARCA's backend-selection precedence, Petitio today vs.
  the designed future, and the request/resolve sequence.
- New `.github/ISSUE_TEMPLATE/adapter-request.yaml` for requesting a new ARCA backend.

## [0.13.1] - 2026-08-14

### Added

- Two new Claude Code skills, installed both in-repo and at Claude Code's user scope alongside
  the existing `portunus-ask`: `portunus-drop` (store a secret you were just handed, single or
  bulk, local-vault only) and `portunus-vault-setup` (configure/check a project's backend +
  sync mode, force a sync). All three now cross-reference each other so an agent lands on the
  right one regardless of which it reaches for first.

## [0.13.0] - 2026-08-14

### Added

- **Per-project/per-reference vault routing** — closes the gap between `backend.py`'s own
  long-standing docstring claim ("selected per-Reference by provider+project") and reality (one
  global `PORTUNUS_BACKEND` env var per process). `Resolver` now routes each reference through a
  3-level precedence: the reference's own `backend` override, else its project's
  `VaultBinding.backend`, else the global `PORTUNUS_BACKEND`-selected backend as the final,
  unchanged fallback. `PORTUNUS_BACKEND=mock` always short-circuits the router entirely — a
  configured binding can never override a test/dry-run's mock mode.
- **`VaultBinding`** (renamed from `GcpProjectBinding`) gains `backend`
  (`local`/`gcp`/`aws`) and `sync_mode` (`direct`/`cached`) fields. Config lives in the new
  `PORTUNUS_HOME/vault-bindings.json`, with a migration-safe fallback to the legacy
  `gcp-bindings.json` (old schema, defaulting to `backend="gcp", sync_mode="direct"` — the real
  vault's existing bindings keep working with zero manual migration).
- **Recency-aware, pull-only sync-down cache** (`SyncingBackend`) — a project with
  `sync_mode="cached"` fetches from GCP once, caches it in the local encrypted vault, and only
  re-fetches when the remote's value has actually changed (a cheap `gcloud secrets versions
  describe` recency check on every access). GCP → local only, never the reverse. `portunus sync
  <project>` / `portunus_sync` (MCP) force an explicit recency check ahead of relying on
  incidental access timing — useful for materializing a fresh set of secrets once at deploy time.
- **`portunus bindings set --backend/--sync-mode`**, extended `bindings show`/
  `portunus_bindings_show` output.
- **`portunus_drop_bulk`** (MCP) / **`portunus drop-bulk <file.json>`** (CLI) — create many
  local-vault secrets in one call, motivated by a real "try ~100 candidate passwords safely"
  workflow. A malformed entry is reported separately without aborting the rest of the batch.
  `portunus_drop`/`drop` also gain the `backend` override parameter their own design had
  described but hadn't wired up yet.
- **UI**: Project Explorer's read-only WIF badge becomes a real per-project backend/sync_mode
  editor (new `/api/bindings` route). DetailDrawer gains a distinct, inert "Auto-rotate…" stub
  signaling the future real-key-rotation direction, kept separate from the existing, working
  manual "Rotate…" flow.

### Fixed

- Project Explorer's `load()` no longer lets one failing endpoint (`discover`, now an expected
  failure for a local-only project with no live GCP project at all) block the other two
  (`list`/`bindings`) from rendering — found via live browser testing during this release.

## [0.12.0] - 2026-08-14

### Added

- **`portunus_drop`** — the create-side counterpart to the read/inject MCP tools, letting a
  handed-off agent instance create a new secret in the local vault end-to-end without shelling
  out to the CLI. Local-vault only (fails closed with the identical message the CLI's own
  `drop` uses if the backend is `gcloud`/`aws` — Portunus still has no write path into GCP
  Secret Manager or AWS). Lands new references at `state=dropped`, mirroring `cmd_drop` exactly.
  `value` is the one argument across the whole MCP surface that flows *in* from the caller's
  context rather than out of Portunus — inherent to being handed a secret to store, not a
  boundary violation (see README's MCP section for the full reasoning). Three layers of
  scrutiny given the risk: an AST check confirming no `Return` expression references the
  `value` name, line-by-line review, and an explicit docstring instruction telling the caller
  not to echo the value back to the human after a successful store.
- **`portunus_state`** — a thin MCP wrapper over `Registry.set_state()`, mirroring `cmd_state`.
  Closes the loop `portunus_drop` deliberately leaves open (fail-closed `state=dropped` by
  default) — promotes a freshly-created secret to `state=enabled` without a CLI round-trip.
  Pure metadata; no backend or value ever touched.
- Installed the `portunus-ask` skill at Claude Code's user scope (`~/.claude/skills/`) in
  addition to this repo's copy, so other Claude Code instances see it automatically —
  previously it was only visible inside this one repo.

## [0.11.0] - 2026-08-13

### Added

- **`portunus mcp`** — a stdio [MCP](https://modelcontextprotocol.io) server, a third OSTIARIUS
  entry point (alongside the CLI and the standalone UI) so other agents/harnesses can query and
  inject Portunus secrets directly, in-process, without ever asking a human for a key. Eight
  tools: `portunus_health`, `portunus_list`, `portunus_tree`, `portunus_ask_preview`,
  `portunus_bindings_show`, `portunus_discover`, `portunus_resolve_to_tempfile`, and
  `portunus_resolve_exec` — the last two are the injection tools, both using the same dual
  `name`/`tags` addressing as the CLI's own `inject`/`ask`, never raw `{{secret:NAME}}` syntax.
- **`portunus_resolve_exec`** — the "make the call for me" tool. Runs a caller-supplied command
  with a `{{secret}}` marker substituted through a capturing `subprocess.run` (30s timeout, not
  the CLI's default `execvp`) and returns only `{stdout, stderr, returncode}` — never the
  resolved command line, on any path including timeouts/exceptions. Verified live against the
  real Google Generative AI API using the real `demo-project-483920` Gemini key: a real API
  response came back, the key itself never appeared in the tool's result.
- **`portunus auth login <email>`** / **`portunus auth status [--json]`** — bounded auth
  lifecycle through Portunus instead of bare `gcloud`. `login` is a thin wrapper around
  `gcloud auth login` (still opens a real browser); `status` cross-references every configured
  `gcp-bindings.json` account against `gcloud auth list`'s credentialed accounts, per-binding.
  Not automatic reauth by design — a single control surface, not a reauth guarantee.
- Registered `portunus mcp` in this environment (`claude mcp add --scope user portunus`) and
  verified end-to-end via a raw JSON-RPC handshake/tools-list/tools-call script (this session
  cannot attach to a server registered mid-conversation).

## [0.10.0] - 2026-08-13

### Added

- **`group`/`related` metadata.** `Reference` gains a hierarchical `group` path (e.g.
  `project-y/supabase/auth`) and explicit `related` cross-references to other reference names
  -- additive, excluded from tag-matching, same precedent as description/purpose. `--group`/
  `--related` on `reg add`/`drop`/`retag`.
- **`portunus tree [--project X] [--json]`** -- the LLM-facing hierarchy/relationship query.
  Renders every reference's group as a real tree; a reference with no group renders under an
  `(ungrouped)` bucket rather than disappearing (the common case for freshly-discovered
  projects). `related` entries not in the current result set are marked `(unresolved)`, never
  dropped or erroring.
- **UI: DetailDrawer** shows and edits group/related through the same Move form/retag path.
- **UI: Project Explorer's Registered list now renders as a nested tree**, built client-side
  from the same data already fetched -- an independent TypeScript implementation of the same
  grouping rule as the Python CLI, verified to agree exactly against real data.
- Applied to the real vault: `demo-project-483920`'s Resend key pair grouped and
  cross-linked; **all 342 real `demo-cicd` secrets organized into ~20 real application groups**
  (event-api/dev+prod, social-engine/dev+prod, shindig, game-library, monitoring,
  orchestration, venues, stripe, and more) by naming convention, replacing an undifferentiated
  flat list with a real navigable structure in both the CLI and the live UI.

## [0.9.0] - 2026-08-13

### Fixed

- **Multi-account GCP bindings.** Found live: authenticating a second GCP account
  (`gcloud auth login`) silently broke access to every project governed by the first, because
  no Portunus GCP code path passed an explicit `--account=` -- everything followed gcloud's
  single mutable "active account" pointer. Both credentials were never actually lost (`gcloud`
  already stores multiple accounts simultaneously); the fix makes every GCP call explicit.

### Added

- `GcpProjectBinding` gains `account: str` -- a local gcloud CLI identity to use per project,
  mutually exclusive with a WIF binding on the same project (a minted token already carries
  identity). `GcloudBackend.access()` (the real value-fetch/injection path) and
  `discover.py::list_gcp_secrets()` (the exact path that broke live) both pass `--account=`
  when configured.
- **`portunus bindings set/show`** -- previously no CLI command existed to configure
  `gcp-bindings.json` at all (only `save_gcp_bindings()`, called by tests). `set` is an
  upsert (only passed fields change); `show` prints real account/WIF-audience values (a
  local-CLI-reading-its-own-config trust boundary, deliberately different from the UI's
  presence-only `wif_configured`).
- Verified live against two real GCP accounts in one session:
  `portunus discover --project demo-cicd` (342 secrets, `account=work@example.com`)
  immediately followed by `portunus discover --project demo-project-483920` (36 secrets,
  `account=personal@example.com`), both succeeding regardless of which account gcloud
  considered "active" -- and confirmed the pre-fix failure mode by removing the binding and
  reproducing the exact `Permission denied` error the user hit.

## [0.8.0] - 2026-08-13

### Added

- **Metadata write support.** `portunus reg add`/`drop` gain `--description`/`--purpose`/
  `--injected-as`; `Registry.retag()` can now update all three in place (no collision check --
  they're not tag-matchable). `--injected-as` reuses the existing `_parse_tags()` helper.
- **UI: metadata display + edit.** Console/Vault Map/DetailDrawer show description/purpose/
  injected_as when set; DetailDrawer's existing Move form gains the same three fields,
  editing through the same `portunus retag` path -- no second write surface.
- **`portunus discover --json`**, plus a new `wif_configured` boolean field (from
  `load_gcp_bindings()` -- never the WIF audience string itself).
- **UI: Project Explorer** (new third tab) -- a GCP-project-scoped view combining what's
  already registered (`/api/list`), what's discoverable in live GCP Secret Manager
  (`/api/discover`) with a single "Register all" action (the CLI's `--register` has no
  per-secret selection, so the UI never implies one), and a GCP-WIF-configured indicator.

## [0.7.0] - 2026-08-13

### Added

- **Richer secret metadata.** `Reference` gains `description`/`purpose`/`injected_as`
  (`{env: "env:VAR"|"file:path"}`) -- additive, non-tag-matchable, round-trips through
  existing registry files unchanged.
- **Keyless GCP backend (Workload Identity Federation).** Ported the tested `auth.py`
  module (`GCPWorkloadIdentityAuth`, `AWSWebIdentityAuth`, `assert_no_long_lived_cloud_keys`)
  from this repo's own `dos-81-keyless-wif` branch. `GcloudBackend` is now multi-project
  aware -- `PORTUNUS_HOME/gcp-bindings.json` (`0600`) maps project -> WIF audience; two
  references pointing at different GCP projects each mint against their own binding in
  the same process. Access tokens are 0600-tempfile-then-unlink, never logged/printed/
  returned. `portunus auth gcp [--project]` reports identity/scope/expiry only.
- **GCP secret discovery.** `portunus discover --provider gcp --project <id> [--register]`
  -- read-only enumeration of what already exists in a live GCP Secret Manager project
  (names + labels + create-time, never a value; `discover.py` holds no reference to any
  backend's `access()` method at all). `--register` writes not-yet-registered secrets as
  `state=requested` placeholders, description seeded from GCP labels, local name derived
  as `<project>-<sm-name>` to avoid cross-project collisions, never overwrites an existing
  reference. Manually validated against two real GCP projects.
- **LLM-facing "list keys for project" query.** `Registry.list_by_project()` -- metadata
  only, zero-to-many, a sibling method to `resolve_by_tags()` rather than an overload of
  it. `portunus list --project <id>` and `portunus ask "what secrets are available for
  X"` (new `list` intent kind, alongside fetch/add/rotate).
- **AWS Secrets Manager backend stub.** `AWSSecretsManagerBackend` -- `access()` raises
  clearly, zero AWS SDK/network calls. `PORTUNUS_BACKEND=aws` selects it; fixes a real
  gap found during implementation where an unrecognized backend kind silently fell
  through to the local-encrypted default instead of failing closed.
- README's ARCA/OSTIARIUS sections now narrate the full multi-backend vision (pluggable
  stores selected per-Reference by provider+project) with a worked discovery example.

## [0.6.0] - 2026-08-13

### Added

- **L2 plugin lifecycle groundwork (scope 1 of 2).** `GET /api/health` -> `{"status":"ok"}` --
  a trivial liveness signal, never touches the CLI/subprocess. `next.config.mjs` gains
  `output: "standalone"` so `node .next/standalone/server.js` is a single fixed entrypoint the
  Pantheon host can supervise (`start: node`), matching Janus/Consus/Mnemosyne. `manifest.json`
  gains a `capabilities` list and `health_endpoint`.
  Researched directly against `mdostal/pantheon-v2`'s actual contract code. Registering these
  facts in that repo's shared manifests (`pantheon.gods.yaml`, `plugins.manifest.yaml`,
  `docs/PORTS.md`) is scope 2 -- a separate PR against that repo, for operator review.

## [0.5.1] - 2026-08-13

### Added

- UI: a `⟳ rotation requested` badge in Console, Vault Map, and DetailDrawer wherever a
  reference has `tags.rotation_requested=true` (set by an agent's `portunus ask "rotate ..."`
  request, v0.3.0) -- closes a display gap, no backend change.

## [0.5.0] - 2026-08-13

### Added

- **CLI exposure for session storage.** `portunus session store|load|inspect|list|remove`
  wires the v0.4.0 session-storage library API into the CLI. `store` mirrors `drop`'s
  stdin-only-in discipline; `load` mirrors `resolve`'s tempfile-only-out discipline exactly --
  a session record contains real cookies/tokens, exactly as sensitive as a secret value, so it
  gets a `0600` temp file and only the path is ever printed. `inspect`/`list` are metadata-only.
  `store`/`load`/`remove` write audit entries; `inspect`/`list` don't, matching the existing
  unaudited-read convention.

## [0.4.0] - 2026-08-13

### Fixed

- **`LocalEncryptedBackend.load_session()` now enforces its own TTL.** Previously an expired
  session was returned identically to a valid one -- `load_session()` never checked its own
  `ttl.expires_at` metadata. Raises `SessionExpired` (a `BackendError`) by default;
  `allow_expired=True` opts out for metadata-only callers.

### Added

- `list_sessions()` -- enumerates every stored session's metadata (namespace/TTL/rotation/
  `expired`), never a session payload. Skips corrupt/undecryptable entries rather than failing.
- `inspect_session()` gains an `expired: bool` field.
- CONTEXT.md documents session-storage vocabulary (`SESSION_SCHEMA`, `store_session`, etc.)
  that shipped before this release (PAN-7831) but was never written down.

## [0.3.0] - 2026-08-13

### Added

- **Agent-initiated add/rotate requests.** `portunus ask` now recognizes add/rotate language
  (`intent_kind`, `intent.py`) and routes to a *request*, never a fulfillment — an agent can
  ask for a secret to be added (`--name`/`--tags` required — free text can't safely name
  something brand new) or flag an existing one for rotation, without ever supplying or seeing
  a value. Add creates a `state=requested` placeholder (`Registry.request()`); rotate sets a
  metadata marker (`Registry.retag()`) on the existing reference. Fulfillment still requires a
  human running `portunus drop`.
- **Move/re-tag.** `portunus retag <name> --provider/--project/--env/--tags` updates a
  reference's tags in place, rejecting any change that would collide with a different existing
  reference. A Move action in the UI's DetailDrawer does the same via a new `/api/retag` route.
- **`requested` lifecycle state** — fails closed via `Broker.check_injectable` exactly like
  `dropped`/`revoked`. Found and fixed a related gap: `check_injectable` used a
  dropped/revoked *denylist* that would have silently treated `requested` (or any future new
  state) as injectable; flipped to an enabled/locked *allowlist* so new states fail closed by
  default.
- **`--home <path>`** — explicit, per-invocation vault override for `PORTUNUS_HOME` (cross-repo
  targeting). Not automatic multi-vault federated search, which remains out of scope.
- UI: a distinct `requested` state pill in Console/Vault Map.

## [0.2.0] - 2026-08-13

### Added

- **Metadata-tag lookup.** `Reference` gains structured `provider`/`project`/`env` fields plus
  an open `tags{}` dict, alongside the existing `scope`/`kind`. `Registry.resolve_by_tags()`
  resolves a partial tag query to exactly one reference, or fails closed with `NoMatch`/
  `AmbiguousMatch` — never a guess. Existing references migrate additively via
  `migrate_legacy_tags()`. A coarse file lock now serializes registry writes.
- **`portunus find --tags ...`** — CLI lookup by tags, metadata only, never a value.
- **Boundary injection adapters.** `portunus inject --tags ... --target env|file` injects a
  resolved secret directly into a process environment variable or a templated `.env`/JSON/YAML
  file, via a new `SecretAdapter` abstraction (`EnvVarAdapter`, `FileAdapter`,
  `HttpHeaderAdapter`, `HttpBodyAdapter`). Every injection is boundary-safe (the value is never
  returned, printed, or logged, including on the failure path) and produces an
  `adapter_resolution` audit entry.
- **Semantic front door.** `portunus ask "<plain-language request>"` maps free text to a tag
  set via deterministic matching against the registry's own known vocabulary (`intent.py`),
  then resolves and optionally injects. Fails closed at both the parsing and resolution layers
  — never guesses. Ships with a thin Claude skill (`.claude/skills/portunus-ask/`) so an agent
  can invoke it as a tool call.
- **Standalone UI (`ui/`).** A localhost-only Next.js app: Console (default tab — faceted
  table + detail/audit drawer), Vault Map (second tab — cards grouped by provider/project), and
  a persistent Ask Bar side panel. Every API route shells out to the same gated `portunus`
  console script rather than reimplementing any gating logic in TypeScript. The add-secret
  form is the sole human-plaintext-entry point, mirroring `portunus drop --stdin`.
- `portunus audit --json [--secret SM_NAME]` — machine-readable audit output for UI/tooling
  consumers.
- `portunus drop` now accepts `--provider`/`--env`/`--tags` so newly-added secrets can carry
  the full tag schema.

### Changed

- `portunus ask` without `--target` is now a resolve-only preview (success, not an error) —
  lets a caller see the match before committing to an injection target.

## [0.1.0] - 2026-07-07

Initial release: reference registry, OSTIARIUS boundary-only resolver, Petitio approval-gate
broker, ARCA backends (local-encrypted default, GCP Secret Manager, mock), tamper-evident
audit chain, and the `portunus` CLI (`reg`, `drop`, `resolve`, `gate`, `approve`, `grant`,
`state`, `status`, `audit`, `verify`). Arca session vault storage (TTL/rotation-metadata
browser session persistence) added in the same line via PAN-7831.
