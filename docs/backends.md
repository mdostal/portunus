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
| `backend` | `gcp` (default) — which backend adapter handles this project |
| `sync_mode` | `direct` (live fetch every access) or `cached` (pull-only sync-down; see below) |

### Sync mode: cached

`sync_mode: cached` wraps the GCP backend in a `SyncingBackend` that:

1. Checks the remote version marker (via `gcloud secrets versions describe latest`) to see if anything changed since the last sync.
2. If the marker matches, serves the value straight from the local encrypted cache — zero remote value-fetch.
3. If the marker differs (or no cached copy exists), fetches the real value from GCP, stores it locally, and updates the marker.
4. If the remote is unreachable (network/DNS/timeout), serves the last-known-good local copy rather than failing — **offline resilience**.

Sync is always **pull-only**: local-to-GCP pushes never happen through this path.

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

# Check which configured bindings have a locally-credentialed gcloud account
portunus auth status
```

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

## Stub backends

The following backends are recognized by Portunus but not yet implemented. Each fails closed with a clear error rather than silently mis-routing to a different backend:

| Backend | Backend identifier |
|---------|-------------------|
| AWS Secrets Manager | `aws` |
| HashiCorp Vault / OpenBao | `vault` |
| Azure Key Vault | `azure` |
| 1Password Secrets Automation | `onepassword` |
| Doppler | `doppler` |
| Infisical | `infisical` |

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
| Any other cloud provider | Wait for the stub to be implemented |

A single vault can mix backends per-reference using VaultBindings — a development reference on `local` and a production reference on `gcp` coexist without conflict.
