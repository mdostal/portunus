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
| `grafana-cloud` (org access policy token) | Grafana Cloud access policy token (`glc_...`) | **auto** | `GrafanaCloudRotationAdapter`: create a sibling token under the same access policy → verify (read-only GET with the new token) → store. `--retire-old` **deletes** the old token — the API has no disable. Needs `grafana_access_policy_id` + `grafana_region` tags; see [Grafana Cloud access policy tokens](#grafana-cloud-access-policy-tokens). **Not** a self-hosted Grafana admin password (manual) or a stack data-source credential (unknown). |
| `gcp` (service-account key) | Service account JSON key | **auto** | `GCPServiceAccountKeyRotationAdapter` drives the full `create → verify → store` cycle. Retirement is opt-in via `--retire-old` (disables the superseded key; deletion is deferred to a future grace-period sweep). The `iam_account` tag on the reference must be set to the service account email. |

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
