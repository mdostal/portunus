# Rotation Guide

This document answers: **what rotates automatically, what doesn't, and what you need to do by hand.**

---

## Non-destructive default

Portunus rotation is **non-destructive by default**: the old credential is never revoked or retired
unless you explicitly pass `--retire-old`. Most rotation operations add the new credential and verify
it first, then optionally retire the old one as a separate step.

**Why**: a mid-rotation failure (network error, provider-side reject, vault write failure) with
a prematurely revoked old credential leaves you locked out. The non-destructive default means
a failed rotation always leaves the old credential valid, so you can retry safely. Use `--retire-old`
only when you have confirmed the new credential works and are ready to revoke the old one.

**OAuth refresh token rotation is a special case**: the provider itself rotates the refresh token
as part of the grant (`--retire-old` is meaningless and rejected — there is no superseded
credential to retire, because the old token is already spent at the moment the provider returns a
new one).

---

## Provider capability matrix

| Provider | Credential type | Capability | Notes |
|---|---|---|---|
| `oauth` (any provider) | OAuth 2.0 refresh token | **auto** | `OAuthRefreshRotationAdapter` wraps `OAuthBackend` — one job drives every stored credential. Provider-returned `refresh_token` rotation is persisted immediately. |
| `vercel` | API token | stub | Not yet implemented. |
| `github` | Personal access token / app token | stub | Not yet implemented. |
| `stripe` | API key | stub | Not yet implemented. |
| `linear` (personal API key) | Personal API key | **manual** | Linear's rotation API covers OAuth *application* tokens only — personal API keys have no programmatic rotation path. Human re-issue required: generate a new key in Linear settings, update the stored credential via `portunus oauth store` (if OAuth-shaped) or `portunus drop --backend local`. |
| `resend` | API key (`re_...`) | **auto** | `ResendRotationAdapter`: create → verify (read-only `GET /api-keys`, sends no email) → store. `--retire-old` **deletes** the old key — Resend has no disable. Needs `tags["resend_api_key_id"]` for `--retire-old`; see [Resend API keys](#resend-api-keys) below. |
| `youtrack` (JetBrains Hub permanent token) | `perm:...` token for the YouTrack REST API | **auto** | `YouTrackRotationAdapter` drives `create → verify → store` against the Hub REST API. `--retire-old` **deletes** the old token (Hub has no disable). Needs a Hub URL tag and `hub_token_id`; see below. |
| `gcp` (service-account key) | Service account JSON key | **auto** | `GCPServiceAccountKeyRotationAdapter` drives the full `create → verify → store` cycle. Retirement is opt-in via `--retire-old` (disables the superseded key; deletion is deferred to a future grace-period sweep). The `iam_account` tag on the reference must be set to the service account email. |
| `sentry` | User / org / internal-integration auth token | **manual** | Sentry's token endpoints accept session auth only and reject any bearer token, so no admin credential can mint or revoke a token. `SentryRotationAdapter` is registered so `rotation run` prints the human re-issue steps. See `provider-rotation-matrix.md`. |

> **Capability vocabulary:**
> - `auto` — a programmatic rotation path exists and Portunus has a real adapter for this credential type.
> - `manual` — no programmatic rotation path exists for *this credential type* (even if other credential types from the same provider are rotatable). **Human re-issue is permanently required** — this is not a backlog item, it is an inherent provider limitation.
> - `stub` — a real adapter has not been built yet; the provider's programmatic rotation story may or may not exist.

---

## OAuth refresh token rotation

OAuth credentials stored via `portunus oauth store` are automatically eligible for periodic
refresh through `run_periodic_oauth_refresh()`. No per-provider or per-credential job is needed
— the single adapter iterates `list_oauth_credentials()` and drives every entry.

What happens on each rotation run:

1. `OAuthBackend.access()` calls the provider's token endpoint with the stored `refresh_token`.
2. A fresh `access_token` is minted (this is what callers normally get at resolution time).
3. If the provider returns a new `refresh_token` in the response (some providers, confirmed live
   for Codex CLI's subscription login against `auth.openai.com/oauth/token`, rotate the refresh
   token on every use), the new token is persisted back via `store_oauth_credential` immediately —
   using the spent token for a future refresh would fail.

**The adapter never implements its own refresh grant.** The same `OAuthRefreshTokenAuth` path
that mints at resolution time is the rotation path — one implementation, no drift.

---

## Manual providers

The following providers have **no auto-rotation path** for their stored credential type.
No adapter exists, and none can be built without a provider-side API that doesn't exist.
Human re-issue is the only option.

### Linear (personal API key)

Linear's API supports rotating OAuth *application* tokens (tokens issued to an OAuth app), but
**personal API keys** (the `lin_api_...` tokens generated in account settings) have no
programmatic rotation endpoint. If you store a Linear personal API key in Portunus, you must:

1. Generate a new key in your [Linear API settings](https://linear.app/settings/api).
2. Re-store it: `portunus drop <name> <sm_name> --backend local` (or via the UI).
3. Revoke the old key in Linear settings.

### GCP service-account keys -- now auto

GCP service-account key rotation is now handled by `GCPServiceAccountKeyRotationAdapter`
via `portunus rotation run <ref-name> [--retire-old]`. The reference must have:

- `provider=gcp` in its registry entry
- `tags["iam_account"]` set to the full service account email
  (e.g. `sa@project.iam.gserviceaccount.com`)

Prefer Workload Identity Federation where possible -- WIF credentials require no rotation.

## Resend API keys

`ResendRotationAdapter` rotates a Resend API key via `portunus rotation run <ref-name> [--retire-old]`
(API: https://resend.com/docs/api-reference/api-keys). The reference needs `provider=resend`, and
its tags control the rotation:

| Tag | Meaning |
|---|---|
| `resend_api_key_id` | Id of the key currently stored. Required for `--retire-old` (a Resend token does not encode its id). The adapter rewrites it to the new key's id after every successful store. |
| `resend_admin_ref` | Name of another reference holding a `full_access` key, used to create and delete keys. Defaults to the reference itself, which only works if the rotated key is `full_access`. |
| `resend_permission` | `full_access` (default) or `sending_access`. Use `sending_access` together with `resend_admin_ref` for least privilege. |
| `resend_domain_id` | Optional; limits a `sending_access` key to one domain. |

Phases:

1. **create**: `POST /api-keys` with the admin key.
2. **verify**: `GET /api-keys?limit=1` with the **new** key. It only reads and sends no email.
   A `full_access` key gets 200. A `sending_access` key gets 401 `restricted_api_key` ("restricted
   to only send emails"), and Resend only sends that for a valid key. Any other answer (invalid,
   not active, suspended) aborts the rotation. Nothing is stored, and the adapter tries to delete
   the new, unused key.
3. **store**: the new token goes to the local backend.
4. **retire** (`--retire-old` only): `DELETE /api-keys/{old_id}`. **This cannot be undone.**
   Resend has no disable, so pass `--retire-old` only after every consumer has the new key.
   Without it, the old key id is added to the `resend` binding's `superseded_key_ids`, and
   `portunus rotation audit` lists it until it is retired.

---

## YouTrack (Hub permanent tokens)

`YouTrackRotationAdapter` rotates the JetBrains Hub permanent token (`perm:...`) that a
YouTrack REST client authenticates with, via `portunus rotation run <ref-name> [--retire-old]`.
The current token authenticates its own replacement -- no separate admin credential.

1. `GET <hub>/api/rest/users/me` with the current token gives the owner's user id.
2. A new token is created under that user with the **same scope** as the current one.
3. `GET users/me` with the new token must return the same user, or the new token is deleted
   and nothing is stored.
4. The new token is stored; with `--retire-old` the old one is **deleted**. Hub has no way to
   disable a permanent token, so this is irreversible; leave it off until consumers have the
   new token.

The reference needs `provider=youtrack` and these tags:

| Tag | Purpose |
|---|---|
| `hub_url` | Hub service URL, e.g. `https://yt.example.com/hub`. Optional if one of the next two is set. |
| `youtrack_url` | YouTrack URL; Hub is taken as its built-in `/hub` (`…/youtrack` → `…/hub`). |
| `youtrack_url_ref` | Name of a reference that stores the YouTrack URL (e.g. `ffe-orchestration-dev-youtrack-base-url`); resolved through the normal resolver. |
| `hub_token_id` | Hub id of the token currently stored. Required for `--retire-old` and to copy its scope. List ids with `GET <hub>/api/rest/users/{userId}/permanenttokens?fields=id,name` (`{userId}` from `GET <hub>/api/rest/users/me?fields=id`). The adapter sets it to the new token's id after each rotation; without `--retire-old` the old id is listed under the `youtrack` binding's `superseded_key_ids`. |
| `hub_scope` | Comma-separated Hub service ids, used only when `hub_token_id` is not set. |

The Hub URL is per install, so the adapter never assumes one.

---

## Stubs (not yet implemented)

The following adapters are registered but raise on `.rotate()`. File an adapter request at
the link in the error message if you need one of these:

- **Vercel** — priority target for the first real key-rotation adapter; not yet built.
- **GitHub** — personal access tokens / app tokens.
- **Stripe** — API keys.

---

## Adding a new OAuth credential

Any credential stored via `portunus oauth store` is immediately eligible for auto-rotation
through the periodic refresh job — no registration step required. The `list_oauth_credentials()`
call that drives the job enumerates them automatically.
