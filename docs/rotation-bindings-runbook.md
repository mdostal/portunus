# Rotation Bindings Runbook

A rotation adapter that ships in code does nothing for a vault until two things exist:

1. a **rotation binding** for its provider (`PORTUNUS_HOME/rotation-bindings.json`), which records
   that the provider is rotated for real, under which account, and which superseded key ids
   still need retiring. `portunus rotation audit` reads this file to report each reference's
   capability. With no binding, every reference looks unrotatable.
2. a **reference shaped for the adapter**: the right `provider` and the tags that adapter needs.

`rotation-bindings show --json` returning `{}` means step 1 has never been done on that vault.
This page covers both steps, with GCP service-account keys first and then the general recipe
for the next provider.

## Run against the right vault

On hive the live vault is the `portunus-home` volume inside the `pantheon-portunus-1`
container. The host's own `~/.portunus` is a different, empty vault. Run every command below
through `bin/pantheon-vault portunus ...` (or `docker exec pantheon-portunus-1 portunus ...`), not
a bare `portunus` on the host. To confirm you're on the right vault, run `portunus reg show`:
it should list the real references.

## GCP service-account keys, end to end

Prerequisites, all inside the Portunus container:

- `gcloud` is installed and authenticated as an identity that holds
  `roles/iam.serviceAccountKeyAdmin` on the target service account. The adapter shells out to
  `gcloud iam service-accounts keys create|list|disable`.
- A **non-production-critical** service account with a JSON key already stored in the vault.
  Rotating it mints a new key and, with `--retire-old`, disables the old one. Anything still
  holding the old key file stops working.

### 1. Register the binding

```sh
portunus rotation-bindings set gcp --status real --account <sa-name>@<project>.iam.gserviceaccount.com
portunus rotation-bindings show --json
# {"gcp": {"status": "real", "account": "<sa-name>@<project>.iam.gserviceaccount.com"}}
```

`account` is a free-text context hint, never a credential. One binding covers the whole
provider: use the service account you are proving rotation on, or the GCP project id if the
provider spans several accounts.

### 2. Shape the reference

```sh
portunus reg show | grep -A8 '<ref-name>'   # check provider and current tags first
# --tags REPLACES every tag: repeat the ones already set
portunus retag <ref-name> --provider gcp \
  --tags 'iam_account=<sa-name>@<project>.iam.gserviceaccount.com,<existing tags...>'
```

Optional tags (see [Rotation Guide](rotation.md#gcp-service-account-keys-now-auto)):
`gcp_key_format=json|base64|private_key`, plus `gcp_key_id=<private_key_id>` for
`private_key` refs, and `project=<gcp-project>`.

### 3. Rotate and retire

```sh
portunus impact <ref-name>                       # who else reads this key
portunus rotation run <ref-name> --retire-old    # create -> verify -> store -> disable old
```

stdout reports only key ids. Without `--retire-old` the old key id is recorded in the
binding's `superseded_key_ids` and `portunus rotation audit` keeps listing it until it is retired.

### 4. Prove it

```sh
gcloud iam service-accounts keys list --iam-account <sa-email> --managed-by user
#  new key: enabled; old key: DISABLED
portunus audit 5 --secret <sm_name> --changes   # a "rotate" entry for the reference
portunus verify                                 # audit hash chain intact
portunus rotation audit                         # reference shows capability "auto"
portunus rotation-bindings show --json          # non-empty, names gcp + account
```

Deleting the disabled key is deliberately left to a later grace-period sweep, never done in
the same call.

## Adding a binding for the next provider

Each of the remaining adapters (`github`, `resend`, `sentry`, `grafana-cloud`, `youtrack`,
`vercel`, `zernio`, plus `neon`, `upstash`, `stripe`, `mongodb-atlas`, `google-api-key`) needs
the same steps:

1. **Pick one low-risk reference** for that provider. Read its section in the
   [Rotation Guide](rotation.md) for required tags, especially the `*_admin_ref` tag. Several
   providers (Vercel, Resend, Grafana, YouTrack) can't create tokens with the token being
   rotated, so they need an admin reference.
2. **Bind the provider:**
   `portunus rotation-bindings set <provider> --status real --account <team/org/project hint>`.
   The provider name must match the adapter key: one of the names listed above, exactly as
   written (e.g. `grafana-cloud`, not `grafana`).
3. **Shape the reference:** `portunus retag <ref> --provider <provider> --tags '...'`. Repeat
   all existing tags, because `--tags` replaces them.
4. **Dry-check first** where the provider offers it (`portunus impact <ref>`; for
   Resend's multi-target flow, `portunus rotation roll <ref> --dry-run`).
5. **Rotate once:** `portunus rotation run <ref> --retire-old`. Leave out `--retire-old` the
   first time if consumers outside Portunus still hold the old value.
6. **Verify:** provider console or API shows the new key and the old one retired;
   `portunus audit --changes` has the `rotate` entry; `portunus verify` passes;
   `portunus rotation audit` reports `auto` for the reference.

Providers with no programmatic path (Linear personal keys, n8n encryption keys, event-source
APIs) should still be bound, with `--status manual`. That way `rotation audit` reports
"human re-issue required" instead of "unknown".
