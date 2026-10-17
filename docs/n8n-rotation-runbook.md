# n8n Rotation Runbook

Self-hosted n8n has two kinds of stored secret, and both are **manual**. No Portunus adapter exists
for them and none should be built. They are startup configuration values, not provider-issued
tokens, so there is no provider API to call.

| Reference (example) | What it is | Capability | Risk |
|---|---|---|---|
| `social-engine-dev-n8n-encryption-key` | `N8N_ENCRYPTION_KEY`, the key n8n encrypts every stored workflow credential with | **manual, attended only** | Highest blast radius in the rotation backlog. Done wrong, every stored n8n credential becomes unreadable. |
| `social-engine-dev-n8n-basic-auth-user`, `social-engine-dev-n8n-basic-auth-password` | `N8N_BASIC_AUTH_USER` / `N8N_BASIC_AUTH_PASSWORD` for the n8n UI | **manual** | Low. But see [Basic auth is gone in n8n 1.0+](#basic-auth-is-gone-in-n8n-10) first: on current n8n these values do nothing. |

!!! danger "Never schedule the encryption-key rotation"
    No cron job, `portunus rotation run` schedule or CI workflow may touch
    `*-n8n-encryption-key`. Rotate it only as a planned operation, with a person watching each
    step and a backup taken first. Re-syncing the secret store to the host is also a rotation:
    if the value in GCP Secret Manager differs from the one n8n started with, a sync **is** a key
    change.

Sources: n8n's [encryption key](https://docs.n8n.io/hosting/configuration/configuration-examples/encryption-key/)
and [CLI commands](https://docs.n8n.io/hosting/cli-commands/) docs,
[`BREAKING-CHANGES.md`](https://github.com/n8n-io/n8n/blob/master/packages/cli/BREAKING-CHANGES.md)
and the [1.0 migration checklist](https://docs.n8n.io/1-0-migration-checklist/). Checked 2026-10-17.

---

## How n8n uses the encryption key

- n8n encrypts every credential saved in its database (SQLite or Postgres) with this key.
- On first launch, n8n generates a random key and saves it in its settings folder, `~/.n8n`
  (the `encryptionKey` field of `~/.n8n/config`). Setting `N8N_ENCRYPTION_KEY` before that first
  launch makes n8n use your value.
- Since n8n 1.25.0 ([`BREAKING-CHANGES.md`](https://github.com/n8n-io/n8n/blob/master/packages/cli/BREAKING-CHANGES.md)):
  a main instance **will not start** if `N8N_ENCRYPTION_KEY` doesn't match the `encryptionKey` in
  the config file, and in queue mode a worker without `N8N_ENCRYPTION_KEY` won't start.
  So the key is stored in **two places**, the env var and the config file on the data volume,
  and both must change together.
- n8n has no rotate or re-key command. The only supported path is the one n8n documents for
  moving to an install with a different key: export credentials decrypted with the old key,
  then import them under the new key.

What rotation does *not* fix: a key that leaked together with a copy of the database. The
attacker already has everything that key decrypts. Re-key, then rotate every **credential stored
inside n8n** at its own provider.

---

## Encryption key: rotation runbook

The order is fixed: **back up → decrypt all → re-encrypt all → restart → verify**. Never restart
n8n on the new key before the import has run, and never store the new key in a place a sync could
push it to a host before step 5.

### 0. Before the window

- Rehearse on a copy first: restore a volume snapshot (or DB dump) into a scratch instance and run
  steps 2-7 against it end to end. Do the real run only after a clean rehearsal.
- Announce downtime. Workflows (webhooks, schedules) fail or are missed while n8n is down.
- Pick a host path for the plaintext export that is **not** on a shared or synced disk, and plan
  to delete it at step 8.
- Not verified yet: whether anything else n8n encrypts with this key (user MFA secrets,
  source-control SSH keys, external-secrets/SSO settings, depending on version and features in
  use) is covered by `export:credentials`. Assume it isn't. List those features on the instance
  beforehand and plan to re-enter or re-enroll them.

### 1. Back up

- Snapshot the data volume / database **and** copy `~/.n8n/config` (it holds the current key).
  On Fly.io: `flyctl volumes snapshots create <volume-id> --app <app>`.
- Back up workflows too: `n8n export:workflow --backup --output=<dir>`.
- Record the current key's secret-store version. **Don't delete or disable it** until step 8.

### 2. Stop traffic

Stop workflows from running against the old key while you work: scale to a single instance,
stop the workers (queue mode), and block webhook ingress if you can.

### 3. Decrypt all (old key)

On the instance, with the old key still active:

```sh
n8n export:credentials --all --decrypted --output=<path>/n8n-creds-decrypted.json
```

This file holds **every n8n-stored credential in plaintext.** Check the credential count in it
against the UI (Credentials page) before going further.

### 4. Generate and stage the new key

```sh
openssl rand -hex 32
```

Add it as a **new version** of the secret, keeping the old version. Stage it on the host
without restarting. On Fly.io: `flyctl secrets set N8N_ENCRYPTION_KEY=... --stage --app <app>`.
A plain `secrets set` without `--stage` redeploys immediately and skips step 5.

### 5. Re-encrypt all (new key)

With n8n stopped (or from a one-off process that shares the data volume):

1. Set `encryptionKey` in `~/.n8n/config` to the new key, so env and config match.
2. Import under the new key. Import keeps each credential's id, so workflows keep their links:

   ```sh
   N8N_ENCRYPTION_KEY=<new> n8n import:credentials --input=<path>/n8n-creds-decrypted.json
   ```

   n8n warns that import **overwrites** records with the same id. Here that's intended: it's
   what replaces the old-key ciphertext.

### 6. Restart

Start n8n (and every worker) with the new `N8N_ENCRYPTION_KEY`. If the main instance refuses to
start with an encryption-key mismatch, the env var and config file disagree: fix that, not the
data.

### 7. Verify

- `n8n export:credentials --all --decrypted --output=<path>/verify.json` succeeds with the same
  credential count as step 3. This proves every record decrypts under the new key. Delete
  `verify.json` straight away.
- Open a few credentials in the UI and run one workflow per credential type that matters.
- Re-enable webhook ingress, workers and schedules.

### 8. Clean up

- Shred the plaintext export(s): `shred -u <path>/n8n-creds-decrypted.json` (or `rm` it and
  rely on the scratch disk being destroyed).
- Disable, then later destroy, the old key's secret version. Keep the step-1 snapshot until
  you're confident, then delete it too: it is still readable with the old key.
- If the rotation was triggered by a leak, now rotate each stored credential at its provider.

### Rollback

Until step 8, rollback is: stop n8n, restore the step-1 snapshot and `~/.n8n/config`, put the
old key back in the env var, start. The old key must still exist at this point, which is why it
isn't deleted until step 8.

---

## Basic auth is gone in n8n 1.0+

n8n 1.0 made user management mandatory, which "removes support for other authentication methods,
such as BasicAuth and External JWT" ([1.0 migration checklist](https://docs.n8n.io/1-0-migration-checklist/)).
On n8n 1.0 or later, `N8N_BASIC_AUTH_ACTIVE`, `N8N_BASIC_AUTH_USER` and
`N8N_BASIC_AUTH_PASSWORD` are ignored. The UI is protected by n8n's own user accounts
(email + password), which live in n8n's database, not in the secret store.

So before rotating a `*-n8n-basic-auth-*` reference, check that basic auth is actually enforced:

```sh
curl -s -o /dev/null -D - https://<n8n-host>/ | grep -i '^www-authenticate: basic'
```

- **No `WWW-Authenticate: Basic` header**: the basic-auth values are dead config. Rotating them
  changes nothing. Retire the references (`portunus drop`) and remove the env vars from the host
  rather than keep a secret that looks like it protects something. The credential that actually
  matters is the n8n **owner account**; manage its password in n8n (Settings → Personal), and
  make sure an owner has been set up at all (see below).
- **Header present** (n8n < 1.0, or basic auth enforced by a reverse proxy in front of n8n):
  rotate as an ordinary dashboard password, below.

### Basic-auth password: liveness and manual rotation

There is no Portunus liveness adapter for this, by design. The manual check:

```sh
curl -s -o /dev/null -w '%{http_code}\n' -u "$USER:$PASSWORD" https://<n8n-host>/
```

`200` = live, `401` = dead. This only means something when the unauthenticated request above gets
a `WWW-Authenticate: Basic` challenge. Otherwise every password returns `200`.

Rotation:

1. Generate: `openssl rand -hex 24`.
2. Add it as a new version of `*-n8n-basic-auth-password` and set it on the host (or proxy).
   If setting it restarts the app (as `flyctl secrets set` does), that's safe, unlike with the
   encryption key.
3. Run the liveness check with the new password (`200`) and the old one (`401`).
4. Update anyone or anything that logs in with it, then disable the old secret version.

The username rarely needs rotating; if it does, rotate it in the same restart as the password.

---

## Observed: Flayr dev instance (2026-10-17)

Read-only probes against `https://flayr-n8n-dev.fly.dev` (from `firefly-events/flayr`
`infra/n8n/`, which sets `N8N_BASIC_AUTH_*` from the `social-engine-dev-n8n-*` secrets and builds
from `n8nio/n8n:latest`):

- An anonymous `GET /` returns `200` with no `WWW-Authenticate` header, so basic auth is **not**
  enforced and the `social-engine-dev-n8n-basic-auth-*` secrets are dead config.
- `GET /rest/settings` reports `userManagement.authenticationMethod: "email"` and
  `showSetupOnFirstLoad: true`. That flag usually means no owner account has been created yet,
  so whoever opens the UI first can claim the owner account. Not verified beyond this one probe.
  Check it, and set up the owner if needed.
- `infra/n8n/README.md` says the encryption key should "never rotate after first save". This
  runbook is the exception that statement needs: rotate only as above, never by just changing the
  secret.

Not checked: the Cloud Run deployment in `.github/workflows/deploy-n8n.yml`
(`social-engine-n8n-{env}`), which sets no encryption key at all. That means the key it uses is
whatever n8n generated into its local `~/.n8n`. Without a persistent volume, that key is lost on
every new instance.
