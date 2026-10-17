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
| `vercel` | Personal / team access token | **auto** | `VercelRotationAdapter`: create → verify (read-only `GET /v5/user/tokens/current` must return the new token's id) → store. `--retire-old` **deletes** the old token — Vercel has no disable. Proactive rotation of a still-valid token only; a dead token is a liveness problem (GH #144). See [Vercel access tokens](#vercel-access-tokens). **Not** a Vercel AI API key (unknown). |
| `github` (classic PAT, `ghp_`) | Personal access token (classic) | **manual** | No create/reissue API — GitHub issues classic PATs through its web UI only. See [GitHub](#github-per-credential-subtype). |
| `github` (fine-grained PAT, `github_pat_`) | Fine-grained personal access token | **manual** | The org API can only *revoke* a fine-grained PAT's access, never create or reissue one. |
| `github` (OAuth App token, `gho_`, your own App) | OAuth App user token | **auto** | `GitHubRotationAdapter` resets it via `PATCH /applications/{client_id}/token`. Needs your OAuth App's client secret; destructive by nature, so requires `--retire-old`. |
| `github` (`gh auth login` token, `gho_`) | gh CLI OAuth token | **manual** | Issued by GitHub CLI's own OAuth App — only GitHub holds that client secret, so reset is impossible. Re-run `gh auth login` and re-store. |
| `github` (GitHub App user token, `ghu_`/`ghr_`) | Expiring user-to-server token | not applicable | Rotated by the OAuth refresh grant — store the bundle via `portunus oauth store` and the `oauth` adapter handles it. |
| `github` (installation token, `ghs_`) | GitHub App installation token | not applicable | GitHub expires it after 1 hour; it's re-minted from the App's private key. Nothing to rotate. |
| `github` (App private key) | GitHub App private key (PEM) | **manual** | Generate/delete is UI-only in the App's settings page. |
| `stripe` | API key | stub | Not yet implemented. |
| `linear` (personal API key) | Personal API key | **manual** | Linear's rotation API covers OAuth *application* tokens only — personal API keys have no programmatic rotation path. Human re-issue required: generate a new key in Linear settings, update the stored credential via `portunus oauth store` (if OAuth-shaped) or `portunus drop --backend local`. |
| `resend` | API key (`re_...`) | **auto** | `ResendRotationAdapter`: create → verify (read-only `GET /api-keys`, sends no email) → store. `--retire-old` **deletes** the old key — Resend has no disable. Needs `tags["resend_api_key_id"]` for `--retire-old`; see [Resend API keys](#resend-api-keys) below. |
| `grafana-cloud` (org access policy token) | Grafana Cloud access policy token (`glc_...`) | **auto** | `GrafanaCloudRotationAdapter`: create a sibling token under the same access policy → verify (read-only GET with the new token) → store. `--retire-old` **deletes** the old token — the API has no disable. Needs `grafana_access_policy_id` + `grafana_region` tags; see [Grafana Cloud access policy tokens](#grafana-cloud-access-policy-tokens). **Not** a self-hosted Grafana admin password (manual) or a stack data-source credential (unknown). |
| `youtrack` (JetBrains Hub permanent token) | `perm:...` token for the YouTrack REST API | **auto** | `YouTrackRotationAdapter` drives `create → verify → store` against the Hub REST API. `--retire-old` **deletes** the old token (Hub has no disable). Needs a Hub URL tag and `hub_token_id`; see below. |
| `gcp` (service-account key) | Service account JSON key | **auto** | `GCPServiceAccountKeyRotationAdapter` drives the full `create → verify → store` cycle. Retirement is opt-in via `--retire-old` (disables the superseded key; deletion is deferred to a future grace-period sweep). The `iam_account` tag on the reference must be set to the service account email. |
| `google-api-key` | Google Cloud API key (`AIza...`) | **auto** | `GoogleCloudAPIKeyRotationAdapter`: copies the old key's restrictions → create → verify (`keys:lookupKey`, plus an optional live probe) → store. `--retire-old` **deletes** the old key, which Google can undelete for 30 days. Refuses a stored value that is a service-account JSON key. Needs `apikeys.googleapis.com` enabled; see [Google Cloud API keys](#google-cloud-api-keys) below. |
| `sentry` | User / org / internal-integration auth token | **manual** | Sentry's token endpoints accept session auth only and reject any bearer token, so no admin credential can mint or revoke a token. `SentryRotationAdapter` is registered so `rotation run` prints the human re-issue steps. See `provider-rotation-matrix.md`. |

> **Capability vocabulary:**
> - `auto` — a programmatic rotation path exists and Portunus has a real adapter for this credential type.
> - `manual` — no programmatic rotation path exists for *this credential type* (even if other credential types from the same provider are rotatable). **Human re-issue is permanently required** — this is not a backlog item, it is an inherent provider limitation.
> - `not applicable` — the credential is short-lived or rotated by some other mechanism; there is nothing for a rotation adapter to do.
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
- optionally `tags["gcp_key_format"]`, when the stored value isn't the raw key file:
  - `json` (default) -- the whole key file, as gcloud writes it
  - `base64` -- base64 of the whole key file (e.g. `*-service-account-base64` refs)
  - `private_key` -- only the `private_key` PEM, for a key stored as split fields (a
    `*-private-key` ref next to a `*-client-email` ref). Rotate the private-key ref only:
    the client email belongs to the service account, so it doesn't change. A bare PEM has
    no key id, so `tags["gcp_key_id"]` must hold the stored key's `private_key_id` for
    `--retire-old`; the adapter rewrites it after each successful store.

  Split refs stay split on purpose: their consumers read separate env vars, and merging them
  into one JSON ref would break every consumer.

Prefer Workload Identity Federation where possible -- WIF credentials require no rotation.

### GitHub (per credential subtype)

Researched 2026-10-16 against GitHub's REST docs ([OAuth app endpoints](https://docs.github.com/en/rest/apps/oauth-applications),
[org PAT endpoints](https://docs.github.com/en/rest/orgs/personal-access-tokens),
[credential revocation](https://docs.github.com/en/rest/credentials/revoke),
[installation tokens](https://docs.github.com/en/apps/creating-github-apps/authenticating-with-a-github-app/generating-an-installation-access-token-for-a-github-app),
[App private keys](https://docs.github.com/en/apps/creating-github-apps/authenticating-with-a-github-app/managing-private-keys-for-github-apps)).
Like Linear, GitHub has no single answer — it depends on which kind of token you stored:

- **Classic PAT (`ghp_`) and fine-grained PAT (`github_pat_`) — manual.** No API creates or reissues
  either. `POST /credentials/revoke` and the org fine-grained-PAT endpoints can only *revoke*; GitHub
  says "new credentials will need to be generated" in the UI. Generate a new token in
  Settings → Developer settings, re-store it, then revoke the old one.
- **OAuth App token (`gho_`) from an OAuth App you own — auto.** `PATCH /applications/{client_id}/token`
  ("reset a token") swaps it for a new one without the user, authenticated with the App's
  `client_id:client_secret`. GitHub says changes "take effect immediately", so the old token stops
  working the moment the reset returns. There's no add-then-retire path, which means the
  non-destructive default can't hold. The adapter therefore:
  1. refuses unless you pass `--retire-old` to acknowledge it;
  2. refuses **before** any API call when the reference doesn't live in the local vault, since it
     couldn't store the result;
  3. checks that the old token is still valid (`POST /applications/{client_id}/token`; reset only
     works on a valid token);
  4. resets, **stores the new token first** (the old one is already dead), then verifies the new
     one with the same check endpoint.

  Tag the reference with `github_client_id` (the App's client ID) and `github_client_secret_ref`
  (the name of a Portunus reference holding the App's client secret, which is resolved through
  the normal boundary-only resolver):

  ```sh
  # --tags replaces the whole tags dict -- include any existing tags you want to keep
  portunus retag my-oauth-token \
      --tags github_token_type=oauth_app,github_client_id=Iv1.abc123,github_client_secret_ref=my-oauth-app-secret
  portunus rotation run my-oauth-token --retire-old
  ```
- **`gh auth login` token (also `gho_`) — manual.** It's issued by GitHub CLI's own OAuth App, and
  only GitHub holds that App's client secret. Re-run `gh auth login` and re-store it. Without the
  two tags above, the adapter treats any `gho_` token this way.
- **GitHub App user token (`ghu_` with a `ghr_` refresh token) — not applicable here.** Store the
  refresh bundle via `portunus oauth store` (token endpoint `https://github.com/login/oauth/access_token`).
  The `oauth` refresh adapter rotates it, and GitHub's rotated `ghr_` is persisted back.
- **Installation token (`ghs_`) — not applicable.** It expires after 1 hour and is re-minted from the
  App's private key.
- **GitHub App private key — manual.** Generating and deleting keys is UI-only.

The subtype comes from the `github_token_type` tag when it's set (`classic_pat`, `fine_grained_pat`,
`oauth_app`, `app_user`, `app_installation`, `app_private_key`). Otherwise the adapter reads the
token's documented prefix inside the resolver boundary. Refusals never make a GitHub API call.

---

## Grafana Cloud access policy tokens

`GrafanaCloudRotationAdapter` rotates a Grafana Cloud **organization-level access policy token**
via `portunus rotation run <ref-name> [--retire-old]`
(API: https://grafana.com/docs/grafana-cloud/developer-resources/api-reference/cloud-api/).
The reference needs `provider=grafana-cloud`, and its tags control the rotation:

| Tag | Meaning |
|---|---|
| `grafana_access_policy_id` | **Required.** Id of the access policy the token belongs to. The new token is created under the same policy, so it has the same scopes. |
| `grafana_region` | **Required.** The policy's region (e.g. `us`, `prod-us-east-0`). Every token endpoint takes it as `?region=`. |
| `grafana_token_id` | Id of the token currently stored. Required for `--retire-old`. The adapter rewrites it to the new token's id after every successful store. |
| `grafana_admin_ref` | Name of another reference holding a token whose policy has `accesspolicies:write` (and `accesspolicies:delete` for `--retire-old`), used to create and delete tokens. Defaults to the reference itself, which only works if the rotated token's own policy grants those scopes. |
| `grafana_token_ttl_days` | Optional. Sets `expiresAt` on the new token. Without it the new token never expires. |
| `grafana_verify_path` | Optional. Overrides the verify call, as a `/api/...` path on `https://grafana.com` (e.g. `/api/orgs/<slug>`). Use it when the policy lacks `accesspolicies:read`. |

Phases:

1. **create**: `POST https://www.grafana.com/api/v1/tokens?region=<region>` with the admin token,
   body `{accessPolicyId, name, displayName[, expiresAt]}`.
2. **verify**: a read-only GET with the **new** token, by default
   `GET /api/v1/accesspolicies/<policyId>?region=<region>`. Only HTTP 200 passes. On failure nothing
   is stored, and the adapter tries to delete the new, unused token.
3. **store**: the new token goes to the local backend.
4. **retire** (`--retire-old` only): `DELETE /api/v1/tokens/<old_id>?region=<region>`. **This cannot
   be undone.** Pass `--retire-old` only after every consumer has the new token. Without it, the old
   token id is added to the `grafana-cloud` binding's `superseded_key_ids`, and
   `portunus rotation audit` lists it until it is retired.

**Out of scope for this adapter:** a self-hosted Grafana instance's admin password is `manual`
(changing it requires the current password), and Grafana Cloud stack-scoped data-source credentials
(Prometheus remote-write token, Loki push credentials) are `unknown`. They use a different API surface
and need their own research.

---

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

## Google Cloud API keys

`GoogleCloudAPIKeyRotationAdapter` rotates a Google Cloud API key (`AIza...`, managed by
`apikeys.googleapis.com`) via `portunus rotation run <ref-name> [--retire-old]`
(API: https://cloud.google.com/api-keys/docs/reference/rest). The reference needs
`provider=google-api-key`. This is **not** the `gcp` provider: an API key has no private key and
does not act as a service account, so it has its own adapter.

The adapter checks the stored value's format before any API call. A service-account JSON key is
refused with a pointer to provider `gcp`. Any value that is not an `AIza...` key string is refused
too. So a reference whose name doesn't say which kind it is (for example `flayr-vertex-ai-*-key`)
can be tagged `google-api-key`, and the adapter stops rather than rotating the wrong kind.

**Prerequisites:** `apikeys.googleapis.com` must be enabled on the key's project. The calling
identity needs `apikeys.keys.create`, `apikeys.keys.get`, `apikeys.keys.getKeyString`,
`apikeys.keys.lookup`, `apikeys.keys.delete`, and `serviceusage.operations.get` to poll the create operation. The predefined role `roles/serviceusage.apiKeysAdmin`
grants them. If the old key is bound to a service account (`serviceAccountEmail`), the new key is
bound to the same one, which may also need `iam.serviceAccounts.actAs` on it.

| Tag | Meaning |
|---|---|
| `google_api_key_admin_ref` | Name of a reference whose value is a Google OAuth2 access token (`cloud-platform` scope) for the identity above. Resolved through `Resolver.resolve_call()`. If unset, the adapter uses `gcloud auth print-access-token` (the ambient gcloud login). |
| `google_api_key_name` | Resource name of the key currently stored (`projects/<number>/locations/global/keys/<id>`). Optional: without it the adapter finds the key with `keys:lookupKey` on the stored value. The adapter rewrites it to the new key's name after every successful store. |
| `google_api_key_verify_url` | Optional. A read-only URL the key is allowed to call, such as `https://generativelanguage.googleapis.com/v1beta/models` for a Gemini key. The new key must get a 200 from it, sent in `x-goog-api-key`, before it is stored. The call is retried for about a minute while the new key propagates. |

Phases:

1. **find old**: get the old key (`GET .../keys/<id>`). Its `restrictions` (API targets and
   referrer, IP or app limits) and `serviceAccountEmail` are copied to the new key, so a rotation
   never widens what the key can call.
2. **create**: `POST projects/<number>/locations/global/keys`. This is a long-running operation,
   which the adapter polls until done. Then `GET .../keys/<new-id>/keyString`.
3. **verify**: `keys:lookupKey` on the **new** string must return the new key's name. The
   `google_api_key_verify_url` probe runs as well if that tag is set. On failure nothing is
   stored, and the adapter tries to delete the new, unused key.
4. **store**: the new key string goes to the local backend.
5. **retire** (`--retire-old` only): `DELETE .../keys/<old-id>`. Google keeps a deleted key for
   30 days, and `gcloud services api-keys undelete <id>` restores it in that window. Without
   `--retire-old`, the old key name is added to the `google-api-key` binding's
   `superseded_key_ids`, and `portunus rotation audit` lists it until it is retired.

---

## Vercel access tokens

`VercelRotationAdapter` rotates a Vercel personal or team access token (e.g. `vercel-deploy-token`)
via `portunus rotation run <ref-name> [--retire-old]` (API: https://vercel.com/docs/rest-api,
"Authentication" endpoints). The reference needs `provider=vercel`, and its tags control the
rotation:

| Tag | Meaning |
|---|---|
| `vercel_token_id` | Id of the token currently stored. Optional: when absent, the adapter asks Vercel for it (`GET /v5/user/tokens/current` with the stored token) before creating anything. The adapter rewrites it to the new token's id after every successful store. |
| `vercel_admin_ref` | Name of another reference holding a token allowed to create and delete tokens. Defaults to the reference itself. |
| `vercel_team_id` / `vercel_team_slug` | Optional; create the new token on behalf of that team (`teamId` wins). |
| `vercel_project_id` | Optional; scopes the new token to one project. |
| `vercel_token_ttl_days` | Optional; sets `expiresAt` on the new token. Without it the new token never expires. |

Phases:

1. **create**: `POST /v3/user/tokens` with the admin token. Vercel returns the new token's value
   only once, in this response.
2. **verify**: `GET /v5/user/tokens/current` with the **new** token. It only reads. It must answer
   200 with the id that create returned. Any other answer aborts the rotation. Nothing is stored,
   and the adapter tries to delete the new, unused token.
3. **store**: the new token goes to the local backend.
4. **retire** (`--retire-old` only): `DELETE /v3/user/tokens/{old_id}`. **This cannot be undone.**
   Vercel has no disable, so pass `--retire-old` only after every consumer has the new token. If
   the old id is neither tagged nor discoverable, `--retire-old` refuses before creating anything.
   Without it, the old token id is added to the `vercel` binding's `superseded_key_ids`, and
   `portunus rotation audit` lists it until it is retired.

**Rotation vs. liveness (GH #144).** This adapter is for rotating a token *before* it expires or
is revoked. A token that is already dead (`invalidToken: true`) cannot authenticate the create
call, so it can't rotate itself: point `vercel_admin_ref` at a different, valid token, or re-issue
it by hand. Detecting dead tokens is liveness's job (`liveness.py`, GH #144). Setting
`vercel_token_ttl_days` and rotating on a schedule keeps tokens from reaching that state.

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

## Liveness checks

Rotation only fixes credentials Portunus *can* rotate. Most stored GitHub tokens are manual, and
they can die silently (expired, revoked, rotated by hand somewhere else). The shared liveness
framework ([liveness.md](liveness.md)) has a `github` probe, so `portunus liveness check` asks
GitHub whether each stored token still works:

```sh
portunus liveness check --all              # every reference with a registered probe, github included
portunus liveness check my-token --json
portunus liveness status --failing         # dead/unknown references, oldest failure first
```

The GitHub probe calls `GET /rate_limit` with the stored token. It doesn't count against the rate
limit and accepts every bearer-token type. `200` means live and `401` means dead. Anything else
(network error, 5xx, 403) is `unknown`, not a guess. App private keys aren't bearer tokens, so
they're reported `unknown` without any request. `liveness check` exits 2 when anything is dead.

---

## Stubs (not yet implemented)

The following adapters are registered but raise on `.rotate()`. File an adapter request at
the link in the error message if you need one of these:

- **Stripe** — API keys.

(GitHub is no longer a stub. See [GitHub](#github-per-credential-subtype) above.)

---

## Adding a new OAuth credential

Any credential stored via `portunus oauth store` is immediately eligible for auto-rotation
through the periodic refresh job — no registration step required. The `list_oauth_credentials()`
call that drives the job enumerates them automatically.
