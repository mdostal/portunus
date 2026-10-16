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
| `github` (classic PAT, `ghp_`) | Personal access token (classic) | **manual** | No create/reissue API — GitHub issues classic PATs through its web UI only. See [GitHub](#github-per-credential-subtype). |
| `github` (fine-grained PAT, `github_pat_`) | Fine-grained personal access token | **manual** | The org API can only *revoke* a fine-grained PAT's access, never create or reissue one. |
| `github` (OAuth App token, `gho_`, your own App) | OAuth App user token | **auto** | `GitHubRotationAdapter` resets it via `PATCH /applications/{client_id}/token`. Needs your OAuth App's client secret; destructive by nature, so requires `--retire-old`. |
| `github` (`gh auth login` token, `gho_`) | gh CLI OAuth token | **manual** | Issued by GitHub CLI's own OAuth App — only GitHub holds that client secret, so reset is impossible. Re-run `gh auth login` and re-store. |
| `github` (GitHub App user token, `ghu_`/`ghr_`) | Expiring user-to-server token | not applicable | Rotated by the OAuth refresh grant — store the bundle via `portunus oauth store` and the `oauth` adapter handles it. |
| `github` (installation token, `ghs_`) | GitHub App installation token | not applicable | GitHub expires it after 1 hour; it's re-minted from the App's private key. Nothing to rotate. |
| `github` (App private key) | GitHub App private key (PEM) | **manual** | Generate/delete is UI-only in the App's settings page. |
| `stripe` | API key | stub | Not yet implemented. |
| `linear` (personal API key) | Personal API key | **manual** | Linear's rotation API covers OAuth *application* tokens only — personal API keys have no programmatic rotation path. Human re-issue required: generate a new key in Linear settings, update the stored credential via `portunus oauth store` (if OAuth-shaped) or `portunus drop --backend local`. |
| `gcp` (service-account key) | Service account JSON key | **auto** | `GCPServiceAccountKeyRotationAdapter` drives the full `create → verify → store` cycle. Retirement is opt-in via `--retire-old` (disables the superseded key; deletion is deferred to a future grace-period sweep). The `iam_account` tag on the reference must be set to the service account email. |

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

## Liveness checks

Rotation only fixes credentials Portunus *can* rotate. Most stored GitHub tokens are manual, and they
can die silently (expired, revoked, rotated by hand somewhere else). `portunus rotation liveness`
asks each provider whether the stored credential still works:

```sh
portunus rotation liveness                 # every reference with a liveness check
portunus rotation liveness --provider github
portunus rotation liveness my-token --json
```

- **GitHub:** `GET /rate_limit` with the stored token. It doesn't count against the rate limit and
  accepts every bearer-token type. `200` means live and `401` means dead; anything else
  (network error, 5xx, 403) is `unknown`, not a guess. App private keys aren't bearer tokens, so
  they're reported `unsupported`. So are references with no stored value (`state: requested`).
- Every result is appended to the audit chain (`liveness <sm_name> ok:live|err:dead|warn:unknown`).
  Response bodies and values are never logged.
- **The command exits 2 if anything is dead**, so a cron job, systemd timer or CI schedule running it
  fails loudly. Portunus has no built-in scheduler yet (tracked in #141), so run this command from
  whatever scheduler you already use.

New providers add one check function to `portunus/liveness.py`'s `_CHECKS`. Autumn (#143), Vercel
(#144) and Clerk (#145) are meant to reuse this runner rather than build their own.

---

## Stubs (not yet implemented)

The following adapters are registered but raise on `.rotate()`. File an adapter request at
the link in the error message if you need one of these:

- **Vercel** — priority target for the first real key-rotation adapter; not yet built.
- **Stripe** — API keys.

(GitHub is no longer a stub. See [GitHub](#github-per-credential-subtype) above.)

---

## Adding a new OAuth credential

Any credential stored via `portunus oauth store` is immediately eligible for auto-rotation
through the periodic refresh job — no registration step required. The `list_oauth_credentials()`
call that drives the job enumerates them automatically.
