## Vault backends and configuration

Portunus routes each secret reference to a backend that fetches the plaintext value at the last possible moment — the injection boundary. The backend is selected per-reference, not globally: a single vault can mix local-encrypted references with GCP Secret Manager ones.

---

## Local encrypted vault

The default backend for development and offline use. No cloud setup required; everything stays on the local machine.

### Storage layout

All state lives under `PORTUNUS_HOME` (env `PORTUNUS_HOME`, or the legacy `DOSTAL_SECRETS_HOME`; defaults to `~/.portunus`):

| File | Contents | Mode |
|------|----------|------|
| `vault.enc.json` | Encrypted secret values (JSON map of name → ciphertext) | 0600 |
| `master.key` | Fernet master key, base64-URL encoded | 0600 |
| `vault.enc.lock` | Serializes concurrent writes | — |

Neither file is ever written with plaintext. The master key is generated once on first use via `cryptography.fernet.Fernet.generate_key()` and never leaves the file.

### Encryption

Portunus uses [Fernet](https://cryptography.io/en/latest/fernet/) from the Python `cryptography` library: AES-128-CBC for confidentiality, HMAC-SHA256 for integrity, a random 128-bit IV per encryption. This is a vetted, high-level recipe — Portunus deliberately does not hand-roll cipher logic.

### Dropping a secret

Use `portunus drop` to store a value. The value must come from stdin or a file — never from an inline argument (inline args appear in shell history and process lists):

```bash
# Read value from stdin (pipe)
echo "my-secret-value" | portunus drop my-ref my-sm-name --stdin

# Read value from a local file the human/harness prepared out-of-band
portunus drop my-ref my-sm-name --value-file /path/to/value.txt

# Interactive masked prompt (for a human at their own terminal, not an agent)
portunus drop my-ref my-sm-name
```

A freshly dropped reference lands in `state=dropped` (not yet injectable). Enable it explicitly:

```bash
portunus state my-ref enabled
```

### Backup and restore

The vault export produces a passphrase-locked archive of all critical state: `registry.json`, `master.key`, `vault.enc.json`, `vault-bindings.json`, and `audit.log`.

```bash
# Export (prompts for passphrase twice; or set PORTUNUS_EXPORT_PASSPHRASE)
portunus vault export --out /path/to/backup.pvault

# Restore (prompts for passphrase; pass --force to overwrite an existing vault)
portunus vault import /path/to/backup.pvault
```

The export command is CLI-only — it is intentionally not exposed as an MCP tool or UI action because a single archive can contain every secret in the vault.

---

## GCP Secret Manager

The production backend for environments with GCP access. Portunus uses `gcloud` CLI calls and supports keyless authentication via Workload Identity Federation (WIF).

### Why no service account key

WIF lets Portunus exchange an OIDC token (e.g., from a CI/CD environment) for a short-lived GCP access token using `gcloud secrets versions access --access-token-file=<tmpfile>`. The token is written to a 0600 temporary file and unlinked in a `finally` block — it is never logged, printed, or returned. No long-lived service account key is provisioned, stored, or rotated.

### Selecting the GCP backend

Set the environment variable before running any Portunus command:

```bash
export PORTUNUS_BACKEND=gcloud
```

Or configure a VaultBinding for specific projects (see below). Per-reference bindings always take precedence over the global environment variable.

### VaultBinding

A `VaultBinding` links a GCP project to its authentication configuration. Bindings are stored in `PORTUNUS_HOME/vault-bindings.json` (0600) and managed with `portunus bindings`:

```bash
# Add or update a binding
portunus bindings set <gcp-project-id> \
  --backend gcp \
  --wif-audience "//iam.googleapis.com/projects/123/locations/global/workloadIdentityPools/my-pool/providers/my-provider" \
  --account user@example.com \
  --sync-mode direct   # or: cached

# Show all bindings
portunus bindings show

# Show one project's binding
portunus bindings show --project my-gcp-project
```

VaultBinding fields:

| Field | Purpose |
|-------|---------|
| `project` | GCP project ID |
| `wif_audience` | WIF provider resource name — used to mint a short-lived access token via OIDC exchange |
| `account` | `gcloud` account email to pass as `--account=`; selects one of multiple locally-authenticated identities independently of `gcloud`'s mutable active-account pointer |
| `impersonate_service_account` | Service account to impersonate via `--impersonate-service-account=`; requires the `account` identity to hold `roles/iam.serviceAccountTokenCreator` |
| `credential_file` | Path to a service account key or `external_account` config JSON, passed to `gcloud` via `CLOUDSDK_AUTH_CREDENTIAL_FILE_OVERRIDE`. A non-interactive identity that a reauth policy can't expire; takes precedence over `account` and composes with `impersonate_service_account`. See [Keyless GCP access](gcp-keyless.md#2c-long-lived-credential-file-no-oidc-issuer-no-stable-account) |
| `backend` | `gcp` (default) — which backend adapter handles this project |
| `sync_mode` | `direct` (live fetch every access) or `cached` (pull-only sync-down; see below) |

### Sync mode: cached

`sync_mode: cached` wraps the GCP backend in a `SyncingBackend` that:

1. Checks the remote version marker (via `gcloud secrets versions describe latest`) to see if anything changed since the last sync.
2. If the marker matches, serves the value straight from the local encrypted cache — zero remote value-fetch.
3. If the marker differs (or no cached copy exists), fetches the real value from GCP, stores it locally, and updates the marker.
4. If the remote is unreachable (network/DNS/timeout), serves the last-known-good local copy rather than failing — **offline resilience**.

Sync is always **pull-only**: local-to-GCP pushes never happen through this path.

The cache entry is keyed `"<project>:<sm_name>"`. A plain `sm_name` key in the local vault is a
value that **lives** locally, written by `drop`, `drop-bulk`, `prompt` or a rotation. Those writes
pin the reference to `backend=local`, so a binding never reroutes them. For a legacy unpinned
reference, when GCP answers NOT_FOUND and a plain local value exists, the router serves the local
value. It warns on stderr and writes a `warn:local-only` audit entry. It does not surface the bare
NOT_FOUND, and it never serves a stale cache entry for a secret GCP no longer has. Auth and network
errors are not masked; they raise with a note that a local copy exists. `portunus health` reports
such references as `routing` degraded, and `portunus doctor --fix-routing` repairs them (#246).

Force a sync for every injectable reference in a project:

```bash
portunus sync --project my-gcp-project
```

### Discover and register secrets

List what exists in a GCP project without fetching any values:

```bash
portunus discover my-gcp-project

# Auto-register any unregistered secrets as state=requested placeholders
portunus discover my-gcp-project --register
```

### Authentication commands

```bash
# Check that WIF token minting works for a project (reports identity/scope/expiry, never the token)
portunus auth gcp --project my-gcp-project

# Authenticate a gcloud account interactively (opens a browser)
portunus auth login user@example.com

# Check every binding's credential is usable right now (exit 1 on missing/expired with --strict)
portunus auth status --strict

# Write a new Secret Manager version through the binding (value on stdin, never argv)
portunus gcp add-version my-gcp-project my-secret --stdin < ./new-value.txt
```

For no-login, no-key-file access (WIF plus a least-privilege service account), see
[Keyless GCP access](gcp-keyless.md).

---

## Chrome saved logins

Reads Chrome's own saved logins (the browser's `Login Data` store) through Portunus's existing resolver boundary — useful for test scenarios that need to exercise real, already-authenticated browser credentials without a separate secret-management step. **Read-only.** Portunus never writes to, rotates, or otherwise touches Chrome's own storage.

**Platform support: macOS and Linux only.** Windows is explicitly not supported — see [#181](https://github.com/mdostal/portunus/issues/181).

### How it works

- **macOS**: Chrome's AES key is derived from a random password stored in the login Keychain (service `Chrome Safe Storage`, account `Chrome`) via PBKDF2-HMAC-SHA1. This is a same-user, no-privilege-escalation read — the same trust tier as Portunus's own local vault.
- **Linux**: the key comes from whichever keyring Chrome itself picked at save time — GNOME Secret Service (`libsecret`) or KWallet, mirroring Chromium's own desktop-environment auto-selection. If neither keyring is available, Chrome stores the password **effectively unprotected** (a real, disclosed Chrome behavior on minimal Linux setups, not a Portunus shortcut) — Portunus surfaces this with an audit warning rather than quietly decrypting it as if it were normal.

### sm_name scheme

```
chrome:<profile>:<origin>:<username>
```

`<profile>` is Chrome's own profile directory name (e.g. `Default`, `Profile 1` — use `portunus browser profiles` to see what's actually there, mapped to the display name you'd recognize in Chrome's own UI). `<origin>` and `<username>` identify one saved login row exactly as Chrome stored it.

### Discovering what's available (metadata only)

```bash
# List every discovered Chrome profile and which store protects it
portunus browser profiles

# List (origin, username) pairs for one profile's saved logins
portunus browser list --profile Default
```

Both commands are structurally incapable of returning a password — they only ever read profile/login *metadata*, never the encrypted value, let alone a decrypted one.

### Registering and resolving a reference

```bash
# Register a reference against a real saved login (the dummy value below is
# never actually used for resolution -- backend=chrome always wins over
# whatever's in local storage, same as backend=oauth)
echo "unused" | portunus drop my-test-login \
  "chrome:Default:https://example.com:alice@example.com" \
  --backend chrome --stdin

# Resolve it through the normal boundary-only path, like any other reference
portunus resolve my-test-login
```

### Known limitations

- **No in-memory cache.** Unlike `OAuthBackend`, every resolve re-reads and re-decrypts — re-decrypting is cheap, and this minimizes how long a decrypted password lives in process memory.
- **Write-back / rotation is explicit backlog, not implemented.** See [#182](https://github.com/mdostal/portunus/issues/182) — the long-term plan is an API-level roll/change rather than mutating Chrome's own SQLite store directly next to a running browser.
- **The Linux key-fetch path was not live-tested** against a real Secret Service/KWallet install during this epic (implemented on macOS) — fixture/mock-only. The macOS path was live-verified against this machine's real Chrome install (1020 real saved logins decrypted with zero failures).

---

## Bitwarden vault

Reads items from your own Bitwarden personal/organization vault (the password manager, not
Bitwarden Secrets Manager) through the official [`bw` CLI](https://bitwarden.com/help/cli/).
Use it to find logins that live in Bitwarden rather than in Portunus, such as social-media
account passwords. **Read-only.** Portunus never writes to your Bitwarden vault.

### Authentication

Portunus never sees your master password or API key. You unlock your own vault and export the
session token in the shell (or service environment) that runs Portunus:

```bash
bw login                          # once per machine
export BW_SESSION=$(bw unlock --raw)
portunus bitwarden status         # -> bitwarden: unlocked (you@example.com)
```

`bw` reads `BW_SESSION` from the inherited environment, so the token never appears on a command
line. If the vault is locked or logged out, every call fails closed with that remediation.
`portunus health` reports the lock state when any reference routes to `bitwarden`.

### Discovering what's there (metadata only)

```bash
portunus bitwarden list --search instagram
portunus bitwarden list --search instagram --json
# Register login items as state=requested references so `portunus search` finds them
portunus bitwarden list --search instagram --register
```

MCP agents use `portunus_bitwarden_search(query, register=False)`. Discovery returns the item id,
name, type, username, URIs, folder/organization ids and custom-field **names** only. `bw list
items` returns full item JSON, values included, so Portunus strips every value as soon as it
parses the output and never returns or prints one.

Registered references are named `bw-<item-name>-<first 8 chars of id>`, tagged `source=bitwarden`
(plus `username` and `uri`), and land in `state=requested`. They can't be injected until you enable
them (`portunus state <name> enabled`). A name collision with a different reference is reported
and never overwritten. Only login items are registered.

### sm_name scheme

```
bitwarden:<item-id>[:<field>]
```

`<field>` defaults to `password`. `username`, `totp` (the current code), `notes`, or any custom
field name on the item also work. To register one by hand:

```bash
echo "unused" | portunus drop my-ig-login \
  "bitwarden:11111111-aaaa-bbbb-cccc-000000000001" --backend bitwarden --stdin
```

### Known limitations

- **Bitwarden Secrets Manager (`bws`, machine-account tokens) is not supported yet.** Only the
  personal/organization vault is.
- **No write-back or rotation.** Change a password in Bitwarden itself.
- **No in-memory cache.** Every resolve calls `bw get`, same rationale as the Chrome backend.
- `bw` serves its local cache; run `bw sync` to pick up items added on another device.

---

## OS keystore (macOS Keychain, Secret Service, Windows Credential Manager)

Resolves a secret from the operating system's own credential store, so a value you already keep
there doesn't need a second copy in the local vault. **Read-only.** Portunus never creates,
changes or deletes keystore items.

| Platform | Store | How Portunus reads it |
|----------|-------|-----------------------|
| macOS | login (or default) Keychain | `security find-generic-password -s <service> [-a <account>] -w` |
| Linux | Secret Service: GNOME Keyring, or KWallet 5.97+ | `secret-tool lookup service <service> [username <account>]` (package `libsecret-tools` / `libsecret`) |
| Windows | Credential Manager, generic credentials | `CredReadW` on target name `<service>` |

The value always comes back on the tool's stdout, never on a command line.

### sm_name scheme

```
keychain:<service>[:<account>]
```

`<service>` and `<account>` are percent-decoded, so write a literal colon in a service name as
`%3A` (e.g. `keychain:https%3A//api.example.com:bob`). Anything after the first unescaped colon
is the account. On Linux the account maps to the `username` attribute, the convention
python-keyring uses, so items stored with `keyring set <service> <user>` resolve as-is. On Windows,
if you give an account, the credential's user name must match it. Otherwise the resolve fails
rather than return a different user's secret.

Without an account, macOS and Secret Service return the first matching item. Give the account
when a service has more than one.

```bash
# macOS: an item created with `security add-generic-password -s github-token -a alice -w`
echo "unused" | portunus drop gh-token "keychain:github-token:alice" --backend keychain --stdin
portunus state gh-token enabled
portunus resolve gh-token
```

### Locked stores fail closed

- **macOS:** a locked keychain, or one that would need a GUI prompt, gives `the macOS keychain is
  locked or access was denied`. Unlock it with `security unlock-keychain` and allow access.
- **Linux:** no Secret Service on the session bus (common over SSH or in containers) gives `no
  Secret Service is reachable`. `secret-tool` exits the same way for a missing item and for an
  unlock prompt you dismissed, so that error names both causes.
- An access tool that hangs on an unlock prompt times out after 30 s and is reported as locked.

None of these fall back to the local vault or to another backend. `portunus health` reports
whether the platform's access tool is available when any reference routes to `keychain`. It
never reads an item, so it can't trigger an unlock prompt.

### Known limitations

- **No discovery or listing.** You address items by service and account; there is no
  `portunus keychain list` yet.
- **No write-back.** Change the item in the keystore itself.
- **No in-memory cache.** Every resolve reads the store again, for the same reason as the Chrome
  backend.
- Tests use mocks only. No live macOS, Secret Service or Windows store was exercised in CI.

---

## Infisical

Reads secrets from [Infisical](https://infisical.com), on Infisical Cloud or self-hosted, using a
**machine identity** with universal auth. **Read-only.** Portunus never writes to Infisical.

### Credentials

Portunus takes the first of these that is set:

1. `INFISICAL_TOKEN`: an access token you already minted for a machine identity.
2. `INFISICAL_UNIVERSAL_AUTH_CLIENT_ID` and `INFISICAL_UNIVERSAL_AUTH_CLIENT_SECRET` (the same
   variable names the Infisical CLI uses).
3. A client id and secret kept in Portunus's own local vault, by default under the sm_names
   `infisical-client-id` and `infisical-client-secret`:

```bash
portunus drop infisical-client-id infisical-client-id --backend local --stdin < ./client-id.txt
portunus drop infisical-client-secret infisical-client-secret --backend local --stdin < ./client-secret.txt
```

The client secret is only ever sent in the body of an HTTPS POST to
`/api/v1/auth/universal-auth/login`. The access token that login returns stays in memory until
shortly before it expires. It is never logged, printed or written to disk. If nothing is
configured, the resolve fails closed and says how to configure it.

Give the machine identity **read** access to each project and environment you resolve from.

### Addressing

**Full address.** This needs no setup beyond credentials:

```
infisical:<project-id>:<environment>:<path/NAME>
```

The last path segment is the secret key and the rest is the folder, `/` by default. For example,
`infisical:6f1c...:prod:/payments/STRIPE_KEY` reads key `STRIPE_KEY` from folder `/payments` in
environment `prod`.

**Project mapping.** Map a Portunus project to an Infisical project and environment once. After
that, references in the project can use plain sm_names (`STRIPE_KEY`, or `payments/STRIPE_KEY`
relative to the mapping's base folder):

```bash
portunus infisical map shop --project-id 6f1c... --env prod --path /backend
portunus bindings set shop --backend infisical     # route the whole project here
portunus infisical status --check                  # mappings, credential source, test login
```

Mappings live in `PORTUNUS_HOME/infisical.json` (mode 0600) and hold topology only: project id,
environment, base folder, and an optional `--site-url` for self-hosted instances. They can also
name different local-vault refs for the client id and secret (`--client-id-ref`,
`--client-secret-ref`) when projects use different machine identities. Without a mapping, the
site URL comes from `INFISICAL_SITE_URL` (or `INFISICAL_API_URL`; a trailing `/api` is dropped),
and defaults to `https://app.infisical.com`.

### Failure behaviour

These all raise a clear error and never fall back to another backend: no credentials, a rejected
login, a denied read (the identity has no access to that environment or folder), an unreachable
server, and a malformed response. If a cached token is rejected, Portunus logs in once more
against the same server before failing. A missing secret is reported as `NOT_FOUND`, so a
reference that follows a binding gets the existing local-value fallback rule (#246), with its
warning and audit entry. A reference pinned with `--backend infisical` and a full address has
no local value to fall back to.

`portunus health` reports, without any network call, how many projects are mapped, where the
credentials come from, and any reference whose project has no mapping.

### Known limitations

- **No write-back, rotation or discovery.** Secrets are addressed by name; there is no `list`.
- Reads use the v3 raw-secrets API (`GET /api/v3/secrets/raw/<name>`), with secret imports
  included. End-to-end-encrypted (non-raw) projects aren't supported.
- Tests use a mocked HTTP transport. It has not been run against a live Infisical instance.

---

## Stub backends

The following backends are recognized by Portunus but not yet implemented. Each fails closed with a clear error rather than silently mis-routing to a different backend:

| Backend | Backend identifier |
|---------|-------------------|
| AWS Secrets Manager | `aws` |
| HashiCorp Vault / OpenBao | `vault` |
| Azure Key Vault | `azure` |
| 1Password Secrets Automation | `onepassword` |
| Doppler | `doppler` |

To request an adapter, use the [adapter-request template](https://github.com/mdostal/portunus/issues/new?template=adapter-request.yaml).

---

## Choosing a backend

| Situation | Recommended backend |
|-----------|-------------------|
| Local development, no cloud account | `local` (default) |
| Testing or offline work | `local` |
| Production with GCP, Workload Identity Federation available | `gcp` |
| Production with GCP, also need offline resilience or deploy-time snapshot | `gcp` + `sync_mode: cached` |
| Testing against real, already-authenticated browser credentials (macOS/Linux) | `chrome` |
| Logins kept in a Bitwarden personal/organization vault | `bitwarden` |
| Secrets managed in Infisical (cloud or self-hosted) | `infisical` |
| Secrets already in the OS keystore (macOS Keychain, GNOME Keyring/KWallet, Windows Credential Manager) | `keychain` |
| Any other cloud provider | Wait for the stub to be implemented |

A single vault can mix backends per-reference using VaultBindings — a development reference on `local` and a production reference on `gcp` coexist without conflict.
