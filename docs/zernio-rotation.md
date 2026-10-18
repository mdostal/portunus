# Zernio Rotation Runbook

`portunus rotate zernio` rotates a Zernio credential **and** pushes the new value to every place it is injected, in an order that keeps webhooks from failing signature checks. It covers two credentials:

| Credential | `liveness_kind` | How it rotates |
|---|---|---|
| Webhook signing secret | `webhook-secret` | Portunus generates it (Zernio never does). The receivers switch first, then Zernio. |
| API key | `api-key` | Zernio's `/v1/api-keys` API: create, verify, store, push, then retire (opt-in). |

The kind comes from the `liveness_kind` tag, or else the reference name's suffix (`...-zernio-webhook-secret`, `...-zernio-api-key`). A reference is picked up by the selectors when its provider is `zernio`, or when it has the tag `liveness_provider=zernio`. Use that tag for references imported under `gcp`.

No value is ever printed, logged, audited or put in an error. Output shows step names, target labels, ids and HTTP status codes only.

---

## What the webhook-secret flow does, in order

1. **Preflight (read-only).** The admin API key reads the webhook record (name, URL, active flag and failure count; never its `secret`). Each target is checked: the variable is listed, but its value is not read back.
2. **Stash.** The current secret is copied to `<sm_name>.previous` in the local vault. This is the rollback point.
3. **Store.** A new 256-bit secret (64 hex characters) is written to the local vault.
4. **Push.** Every target env var gets the new secret. Convex applies a change immediately. A Vercel `production` target is redeployed from its current production deployment, and Portunus waits for `READY`, because Vercel only applies env changes to new deployments.
5. **Switch.** `PUT /v1/webhooks/settings {webhookId, secret}`. Zernio signs with the new secret from here on.
6. **Verify.** `POST /v1/webhooks/test`, then the delivery log must show a 2xx for that test event that the receiver did not reject. The test delivery is identified by its event id, not by its time.
7. **Replay.** Some deliveries can be rejected between step 4 and step 5, while the receivers and Zernio disagree. Each such event (every delivery of it rejected, none later accepted) is redelivered with `POST /v1/webhooks/logs/redeliver`. The report lists the replayed event ids.
8. **Retire (opt-in, `--retire-old`).** `<sm_name>.previous` is dropped. Without it, the previous secret stays available for `--rollback`.

**Why replay is needed.** flayr's `/api/webhooks/zernio` answers a bad signature with **HTTP 200** `{"status":"invalid","reason":"invalid_signature"}`, so that rejections never count toward Zernio's auto-disable. Zernio therefore logs those deliveries as successes and never retries them. Portunus counts a delivery as rejected when its status is not 2xx, or when the receiver's response contains `invalid_signature`, `missing_signature` or `Invalid webhook signature` (the Convex ads route returns 401 with that text). The window between the Vercel deployment going `READY` and the Zernio switch is seconds long, but anything delivered in it would otherwise be lost.

**If verify fails**, the secret has already been switched everywhere Portunus writes. Check the receivers. A Vercel deployment that is still serving the old env is the likely cause. Then either fix it and re-run with `--push-only`, or put everything back:

```sh
portunus rotate zernio --ref social-engine-prod-zernio-webhook-secret --rollback --yes
```

`--rollback` runs the same ordered switch, using the stashed previous secret as the new value. It also stashes the current secret in its place, so a rollback can itself be undone.

**If a push or redeploy fails**, Zernio has not been switched yet and is still signing with the old secret. The error names the targets that were already written. Run `--rollback` to put the old secret back on them.

From the stash until Zernio accepts the new secret, a `<sm_name>.switching` marker sits in the local vault. That window covers a failed push, a failed redeploy and a failed `PUT`. While the marker is there, a new rotation refuses to start, because re-stashing would overwrite the only copy of the secret Zernio may still be signing with. `--rollback` is always allowed, and a completed switch clears the marker.

## What the API-key flow does, in order

1. **Preflight.** The admin key lists the account's API keys. It must be a full key: `zrk_` keys can't manage keys. Each target is checked. If `zernio_api_key_id` is set, the old key must be in that list.
2. **Create.** `POST /v1/api-keys`. The new key copies the old key's `scope`, `profileIds`, `permission` and `disabledResourceGroups`. Zernio returns the key once.
3. **Verify.** `GET /v1/profiles` with the **new** key. A 200 passes. A 403 also passes, because it means a valid key whose `accounts` group is switched off. On failure the new key is deleted and nothing is stored.
4. **Store.** The new key goes into the local vault, and `zernio_api_key_id` is updated in the registry. The old id is recorded as superseded and shown by `portunus rotation audit`.
5. **Push.** Every target gets the new key, and Vercel production is redeployed.
6. **Retire (opt-in, `--retire-old`).** `DELETE /v1/api-keys/{old_id}`. This only happens after every target holds the new key, and it is irreversible because Zernio has no disable.

If a push fails, the old key is still valid and nothing is broken. Fix the target, then run `--push-only`.

**Guided path, when no key can manage keys.** Re-issue the key in the Zernio dashboard, store it with `portunus drop <name> ...`, then run `portunus rotate zernio --ref <name> --push-only`. Revoke the old key in the dashboard last.

---

## Configuration (tags)

| Tag | On | Meaning |
|---|---|---|
| `liveness_kind` | both | `webhook-secret` or `api-key`, when the name doesn't end in one |
| `liveness_provider` | both | `zernio`, for a reference registered under another provider |
| `zernio_targets` | both | **Space-separated** target specs (see below). `--target` (repeatable) overrides them. |
| `zernio_webhook_id` | webhook secret | The Zernio webhook `_id` whose secret this is |
| `zernio_admin_ref` | webhook secret (required); API key (optional) | Name of the reference holding the Zernio API key that makes the API calls. An API key rotates itself by default. |
| `zernio_api_key_id` | API key | Id of the key currently stored. Required for `--retire-old`, and kept up to date by the flow. |

Target specs (`envtargets.py`):

- `convex:<deploy-key-ref>:<VAR>` sets env var VAR on the deployment that the registered deploy key `<deploy-key-ref>` belongs to. The deployment is read from a `prod:`/`dev:` key. For a preview or project key, set `convex_url` on the deploy-key reference.
- `vercel:<token-ref>:<project>[@<team>]:<VAR>[:<target>+...]` sets env var VAR on a Vercel project. `<team>` is a team id (`team_...`) or a slug. Targets are `production` (the default), `preview` and `development`. An existing variable that covers every requested target is updated in place. If that variable also covers other targets, they change too, and the preflight lists them. Otherwise the variable is created with `upsert`. Only a `production` target is redeployed by Portunus. For any other target, or with `--no-redeploy`, Portunus asks you to confirm that you have redeployed, so those runs need a terminal. Vercel *custom* environments (for example flayr's `dev`, which serves dev.flayr.social) are not supported yet.

`portunus retag --tags` **replaces** the whole tag dict, so pass the existing tags too.

## Usage

```sh
# Plan only: preflight every target, change nothing
portunus rotate zernio --webhook 6a73653a0ce2dee3dc80b152 --dry-run

# Rotate, after reviewing the plan (it asks for confirmation; --yes skips the prompt)
portunus rotate zernio --webhook 6a73653a0ce2dee3dc80b152

# API key, selected by env; delete the old key once everything holds the new one
portunus rotate zernio --api-key --env prod --retire-old

# Push the vault's current value everywhere without minting anything
portunus rotate zernio --ref social-engine-prod-zernio-api-key --push-only

# The rotation-run entry point works too (non-interactive, tagged targets only)
portunus rotation run social-engine-prod-zernio-api-key --yes
```

Selectors: `--ref NAME`, or `--webhook ID` / `--webhook-secret` / `--api-key`, optionally narrowed by `--env`. A selector must match exactly one live reference.

## Liveness

`portunus liveness check` probes `api-key` references with `GET https://zernio.com/api/v1/profiles` (Bearer). A 401 means `dead`; a 403 (valid key, group off) means `unknown`. The scheduled `liveness check --all` sweep includes them. A webhook secret is never sent to Zernio's API, so it reads `unknown` and the sweep skips it. The rotation's test delivery is what verifies it.

---

## flayr wiring (`firefly-events/flayr`)

The rows below come from the PANT-1146 ticket and flayr's `CLAUDE.md` / `docs/business/zernio-correspondence/webhook-registration.md` as of 2026-10-18. **Not verified against the live environments.** Confirm each target with `--dry-run` before the first real run.

| Credential | Zernio side | Targets |
|---|---|---|
| Prod webhook secret | webhook `6a73653a0ce2dee3dc80b152` (`flayr-prod-comments` → `https://flayr.social/api/webhooks/zernio`) | Vercel `social-engine` (team `firefly-events`) production `ZERNIO_WEBHOOK_SECRET`. flayr's `CLAUDE.md` also provisions `ZERNIO_WEBHOOK_SECRET` to Convex prod (`pleasant-finch-44`) for `/api/webhooks/zernio/ads`. If that route receives prod deliveries, add the Convex target too. |
| Dev webhook secret | webhook `6a99dd806cc12d1644716f12` (`ffe-dev-ads-p1.5` → `https://calm-gnat-491.convex.site/api/webhooks/zernio/ads`) | Convex dev (`calm-gnat-491`) `ZERNIO_WEBHOOK_SECRET` |
| API key | One key shared by dev and prod (flayr `CLAUDE.md`, verified there 2026-08-22) | Convex prod `ZERNIO_API_KEY`, Convex dev `ZERNIO_API_KEY`, plus any Vercel env that holds it (check before the first run) |

Example setup for the prod webhook secret. The reference names are illustrative, so use the ones in your registry:

```sh
portunus retag social-engine-prod-zernio-webhook-secret --tags \
  "liveness_provider=zernio,zernio_webhook_id=6a73653a0ce2dee3dc80b152,zernio_admin_ref=social-engine-prod-zernio-api-key,zernio_targets=vercel:vercel-deploy-token:social-engine@firefly-events:ZERNIO_WEBHOOK_SECRET convex:social-engine-prod-convex-deploy-key:ZERNIO_WEBHOOK_SECRET"
portunus rotate zernio --webhook 6a73653a0ce2dee3dc80b152 --dry-run
```

The Vercel token reference needs permission to read and write the project's env and to create deployments. The Convex deploy key must belong to the target deployment.

Values are written to the **local** vault, as every rotation adapter does. If a reference is routed to GCP Secret Manager (`ffe-cicd/social-engine/*`), add the new version there by hand afterwards. `portunus parity check <name> --target local --target gcp` (PANT-1151) shows the drift until you do.
