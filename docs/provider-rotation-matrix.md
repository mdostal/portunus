# Provider Rotation Matrix

**Researched**: 2026-09-14  
**Scope**: Every provider represented in the Portunus vault as of this date — local-encrypted vault (Pantheon container) + `ffe-cicd` GCP project (343 secrets). Establishes the real rotation capability for each *stored credential type*, sourced from the provider's own API documentation.

---

## Inventory (enumerated before any provider research)

### Local-encrypted vault (Pantheon container)

`LocalEncryptedBackend.list_oauth_credentials()` — **0 OAuth credentials stored** (no `oauth:*` entries).

`reg json` — 4 references:

| local name | provider | credential type |
|---|---|---|
| `claude-code-oauth` | anthropic | Claude Code OAuth setup-token (~1yr lifetime) stored in GCP SM `pantheon` project |
| `claude-code-oauth-ffevents-hive` | anthropic | Claude Code OAuth setup-token (long-lived), local backend |
| `github-pat-dostal` | github | GitHub PAT (`gho_...` OAuth token via `gh auth login`, repo scope) |
| `dostal-tech-multica-pat-token` | multica | Multica workspace personal access token |

`load_vault_bindings()` — **no bindings configured** in this container.

`load_rotation_bindings()` — **no rotation bindings configured**.

### GCP `ffe-cicd` project

`gcloud secrets list --project=ffe-cicd` — **343 secrets**. Providers inferred from naming conventions (`event-api-prod-stripe-secret-key` → Stripe, `orchestration-dev-linear-api-key` → Linear, etc.).

`personalsites-487021` and `mercatus-liber-commerce` — both returned **0 secrets** (access denied or empty). Not researched further.

---

## Matrix

Each row covers a *provider + credential type* pair. Multiple secrets sharing the same pair are listed as examples. The source URL points to the provider's own documentation for this specific credential type.

---

## `auto` — documented programmatic rotation path exists for this credential type

---

### GCP service account key (JSON)

**Vault examples**: `event-api-dev-firebase-service-account-base64`, `event-api-prod-google-service-account-key`, `social-engine-dev-google-service-account-json`, `social-engine-prod-google-service-account-json`, `orchestration-prod-play-store-sa-key`, `shindig-play-store-service-account-json`, `event-api-dev-firebase-private-key` + `event-api-dev-firebase-client-email` (a split SA key)

**Credential type**: GCP IAM service account key — a JSON file containing `private_key_id`, `private_key`, `client_email`, and related fields; sometimes base64-encoded for transport. Distinct from a Google Cloud API key (a short opaque string — see next row). Firebase SA keys, Google Play SA keys, and any other SA keys authenticating Google services are all this type.

**Source**: https://cloud.google.com/iam/docs/creating-managing-service-account-keys

**Concrete call sequence an adapter would use**:

```bash
# 1. Create a new key for the service account
gcloud iam service-accounts keys create new-key.json \
  --iam-account=<SA_EMAIL>
# REST: POST https://iam.googleapis.com/v1/projects/{project}/serviceAccounts/{account}/keys

# 2. Store the new key JSON in ARCA (replaces the old value in Secret Manager or local vault)

# 3. Verify: make a scoped API call using the new key and confirm success

# 4. Disable the old key (grace period before permanent deletion)
gcloud iam service-accounts keys disable <OLD_KEY_ID> \
  --iam-account=<SA_EMAIL>
# REST: POST https://iam.googleapis.com/v1/projects/{project}/serviceAccounts/{account}/keys/{keyId}:disable

# 5. Delete the old key after the verification window
gcloud iam service-accounts keys delete <OLD_KEY_ID> \
  --iam-account=<SA_EMAIL>
# REST: DELETE https://iam.googleapis.com/v1/projects/{project}/serviceAccounts/{account}/keys/{keyId}
```

The admin credential that calls the IAM API is itself a Portunus-managed Reference, resolved via `Resolver.resolve_call()` — not a special-cased path.

---

### Google Cloud API key

**Vault examples**: `*-google-api-key`, `*-google-maps-api-key`, `*-gemini-api-key`, `*-google-generative-ai-key`, `flayr-vertex-ai-dev-key`, `flayr-vertex-ai-prod-key`

**Credential type**: Google Cloud API key — a short opaque string (`AIza...`) managed by the Cloud API Keys service (`apikeys.googleapis.com`). **Distinct from a service account key** — an API key has no private key component and is not tied to a service account identity; it authenticates the application but does not impersonate a service account.

**Source**: https://cloud.google.com/api-keys/docs/manage-api-keys

**Concrete call sequence**:

```bash
# 1. Create a new API key (with the same API restrictions as the old one)
gcloud services api-keys create --display-name="replacement" \
  --api-target=service=<SERVICE>.googleapis.com
# REST: POST https://apikeys.googleapis.com/v2/projects/{project}/locations/global/keys

# 2. Store the new key string in ARCA

# 3. Verify

# 4. Delete the old key
gcloud services api-keys delete <OLD_KEY_UID>
# REST: DELETE https://apikeys.googleapis.com/v2/projects/{project}/locations/global/keys/{keyId}
```

Note: requires `apikeys.googleapis.com` enabled on the project and an identity with `apikeys.keys.create`, `apikeys.keys.get`, `apikeys.keys.getKeyString`, `apikeys.keys.lookup`, `apikeys.keys.delete` and `serviceusage.operations.get` (all in `roles/serviceusage.apiKeysAdmin`).

**Built (PANT-1136):** `GoogleCloudAPIKeyRotationAdapter`, provider `google-api-key` (separate from `gcp`). It copies the old key's restrictions to the new one. See [rotation.md](rotation.md#google-cloud-api-keys).

Note on Vertex AI keys (`flayr-vertex-ai-*-key`): the name does not say which credential type these are. Checked 2026-10-16 against flayr `development` (`1a28fa9`): no code or secrets script reads `flayr-vertex-ai-*-key`. Flayr authenticates to Vertex AI with the service-account JSON in `social-engine-{env}-google-service-account-json` (`GOOGLE_SERVICE_ACCOUNT_JSON`), and to the Gemini API with the `AIza...` key in `social-engine-{env}-google-generative-ai-key`. The stored values of `flayr-vertex-ai-*-key` were not inspected (that needs vault access). Instead the adapter checks the format at rotation time: tagged `google-api-key`, it rotates an `AIza...` value and refuses a service-account JSON value, which points to `gcp`. `portunus.rotation.classify_google_credential()` gives the same answer for a single value.

---

### Resend API key

**Vault examples**: `event-api-prod-resend-api-key`, `orchestration-dev-resend-api-key`, `social-engine-prod-resend-api-key`, `venues-dev-resend-api-key`, `venues-prod-resend-api-key`

**Credential type**: Resend personal API key (`re_...`), used for transactional email sending.

**Source**: https://resend.com/docs/api-reference/api-keys/create-api-key

**Implemented**: `ResendRotationAdapter` (PANT-1140). The API shape was re-checked against Resend's docs on 2026-10-16: create returns `{id, object, token}`, `permission` is `full_access` | `sending_access`. Verify uses a read-only `GET /api-keys` instead of sending a test email. See [rotation.md](rotation.md#resend-api-keys).

**Concrete call sequence**:

```bash
# 1. Create a new key
POST https://api.resend.com/api-keys
Authorization: Bearer <existing-admin-key>
Body: {"name": "replacement", "permission": "full_access"}
# returns new key value

# 2. Store the new key in ARCA

# 3. Verify (send a test email)

# 4. Delete the old key
DELETE https://api.resend.com/api-keys/{old_api_key_id}
Authorization: Bearer <existing-admin-key>
```

---

### Vercel personal / team access token

**Vault examples**: `vercel-deploy-token`

**Credential type**: Vercel personal or team access token used for CI/CD deployment. **Not** the same as a Vercel AI API key (see `unknown` section).

**Source**: https://vercel.com/docs/rest-api/endpoints/tokens

**Concrete call sequence**:

```bash
# 1. Create a replacement token
POST https://api.vercel.com/v3/user/tokens
Authorization: Bearer <admin-token>
Body: {"name": "replacement-deploy-token"}

# 2. Verify (read-only; must return the id from step 1)
GET https://api.vercel.com/v5/user/tokens/current
Authorization: Bearer <new-token>

# 3. Store new token in ARCA

# 4. Delete old token
DELETE https://api.vercel.com/v3/user/tokens/{tokenId}
Authorization: Bearer <admin-token>
```

---

### YouTrack personal permanent token (JetBrains Hub)

**Vault examples**: `ffe-orchestration-dev-youtrack-api-key`

**Credential type**: JetBrains Hub permanent personal token (`perm:...`), used to authenticate the YouTrack REST API.

**Source**: https://www.jetbrains.com/help/hub/hub-rest-api-users-permanent-tokens-of-user-add-permanent-token-to-permanent-tokens-of-a-user.html

**Adapter**: `YouTrackRotationAdapter` (`provider=youtrack`). See [rotation.md](rotation.md#youtrack-hub-permanent-tokens).

**Concrete call sequence** (the resource is `permanenttokens` under a user id; `scope` is a list of Hub service objects):

```bash
# 0. Resolve the token owner's user id
GET <hub-base-url>/api/rest/users/me?fields=id
Authorization: Bearer <existing-token>

# 1. Create a replacement token (scope copied from the existing token)
POST <hub-base-url>/api/rest/users/{userId}/permanenttokens?fields=id,token
Authorization: Bearer <existing-token>
Body: {"name": "replacement", "scope": [{"id": "<service id>"}]}

# 2. Verify: GET users/me with the new token returns the same id

# 3. Store new token in ARCA

# 4. Delete old token (Hub has no disable)
DELETE <hub-base-url>/api/rest/users/{userId}/permanenttokens/{tokenId}
Authorization: Bearer <new-token>
```

---

### Grafana Cloud organization API key / access policy token

**Vault examples**: `monitoring-dev-grafana-cloud-api-token`

**Credential type**: Grafana Cloud organization-level API key or access policy token, used to manage the Grafana Cloud org (create stacks, manage members, etc.). **Not** the same as a Grafana instance admin password or a stack-scoped data-source token (see `unknown` section).

**Source**: https://grafana.com/docs/grafana-cloud/account-management/authentication-and-permissions/grafana-cloud-access-policies/

**API reference**: https://grafana.com/docs/grafana-cloud/developer-resources/api-reference/cloud-api/

**Concrete call sequence** (using Access Policies API, preferred over legacy API keys). Implemented by
`GrafanaCloudRotationAdapter` (provider `grafana-cloud`), see `docs/rotation.md`:

```bash
# 1. Create a replacement token under the same access policy
POST https://www.grafana.com/api/v1/tokens?region={region}
Authorization: Bearer <admin-token>
Body: {"accessPolicyId": "{policyId}", "name": "replacement"}

# 2. Verify (read-only, with the NEW token)
GET https://www.grafana.com/api/v1/accesspolicies/{policyId}?region={region}

# 3. Store new token in ARCA

# 4. Delete old token
DELETE https://www.grafana.com/api/v1/tokens/{tokenId}?region={region}
```

---

## `manual` — positively confirmed absence of any programmatic rotation path for this credential type

---

### Sentry auth tokens (user, organization, internal integration)

**Vault examples**: `orchestration-dev-sentry-auth-token`, `monitoring-dev-sentry-org-token`, `shindig-prod-sentry-auth-token`, `ffe-cicd-sentry-admin-token`

**Credential type**: any Sentry API token. Sentry has three families: **user auth tokens** (`sntryu_...`, Settings > Account > User Auth Tokens), **organization auth tokens** (`sntrys_...`, Settings > Organization > Auth Tokens, CI-only scopes), and **internal integration tokens** (Settings > Custom Integrations). Tokens created before 2024 have no prefix (64 hex chars).

**Source**: Sentry's own server code, `getsentry/sentry` `master` (checked 2026-10-16):
- `src/sentry/api/endpoints/api_tokens.py` and `api_token_details.py`: user auth tokens
- `src/sentry/api/endpoints/organization_auth_tokens.py` and `organization_auth_token_details.py`: org auth tokens
- `src/sentry/sentry_apps/api/endpoints/sentry_internal_app_tokens.py` and `sentry_internal_app_token_details.py`: internal integration tokens

Every one of these endpoints sets `authentication_classes = (SessionNoAuthTokenAuthentication,)`, which returns unauthenticated for any request that carries an `Authorization` header (Bearer, Basic or otherwise), and marks every method `ApiPublishStatus.PRIVATE`. The user-token endpoint has been session-only since 2023-07-31 (getsentry/sentry#53850); before that it used plain `SessionAuthentication`, so it never accepted token auth. Since 2026-03-17 these endpoints also refuse impersonated sessions (#110654).

**Correction (PANT-1139)**: this section used to list Sentry user auth tokens as `auto`, with `POST /api/0/api-tokens/` authenticated by an `org:admin` bearer token. That call is rejected whatever the token's scopes are. No admin token can mint or revoke another Sentry token, so no unattended create, verify, retire cycle exists. Scripting a browser session (cookie + CSRF + SSO/2FA) against a private API is not a rotation path Portunus will build.

**Which token is which**: the names don't settle the type, but it doesn't change the result. All three families are manual. `*-sentry-org-token` is probably an org auth token. `*-sentry-auth-token` and `ffe-cicd-sentry-admin-token` could be either a user or an org token. `ffe-cicd-sentry-admin-token` (`state: requested`) was presumably meant to be the admin credential for the old `auto` flow, and since that flow doesn't exist it has no rotation role. A one-time prefix check (`sntryu_` vs `sntrys_`) tells you the type if it matters for scope review.

**Human re-issue**:

```text
1. Create the replacement token in the Sentry UI (same page the old one lives on), same scopes.
2. Store it in ARCA under the same reference.
3. Verify with a cheap read the token's scopes allow (e.g. `GET https://sentry.io/api/0/organizations/` for a user token with `org:read`).
4. Revoke the old token from the same UI page.
```

`portunus rotation run` on a `sentry` reference prints these instructions (`SentryRotationAdapter`, capability `manual`).

---

### Linear personal API key

**Vault examples**: `event-api-dev-linear-api-key`, `event-api-prod-linear-api-key`, `orchestration-dev-linear-api-key`

**Credential type**: **Linear personal API key** — a plain string token (`lin_api_...`) generated at https://linear.app/settings/api. **This finding applies specifically to personal API keys. It does not apply to Linear OAuth application tokens, which are a different credential type with a different rotation story.**

**Source**: https://developers.linear.app/docs/graphql/working-with-the-graphql-api#personal-api-keys

Linear personal API keys are created and managed exclusively through the account settings UI. Linear's GraphQL API and OAuth 2.0 endpoint coverage is limited to **OAuth application tokens** (used when a third-party integration authenticates on behalf of a user). No Linear API endpoint exists for issuing or revoking personal API keys.

**Why the distinction matters**: Linear does document OAuth token management (access tokens, refresh tokens) for registered OAuth applications. Reading that documentation and applying its capabilities to personal API keys would be incorrect. The secrets in `ffe-cicd` (`*-linear-api-key`) are personal API keys, not OAuth app tokens.

---

### Anthropic LLM API key

**Vault examples**: `orchestration-dev-anthropic-api-key`, `social-engine-dev-anthropic-api-key`, `social-engine-prod-anthropic-api-key`, `game-library-dev-anthropic-api-key`

**Credential type**: Anthropic API key (`sk-ant-...`) for calling the Claude LLM API — distinct from a Claude Code OAuth setup-token (see next row).

**Source**: https://docs.anthropic.com/en/docs/getting-started/installation

No Anthropic REST API or SDK endpoint exists for creating, listing, or deleting API keys programmatically. Keys are managed exclusively through the Anthropic Console at https://console.anthropic.com/settings/keys.

---

### Anthropic Claude Code OAuth setup-token

**Vault examples**: `claude-code-oauth`, `claude-code-oauth-ffevents-hive` (Pantheon local vault)

**Credential type**: Long-lived Claude Code application OAuth setup-token (~1yr); distinct from a standard Anthropic LLM API key. Used for Claude Code CLI authentication, not for direct LLM API calls.

**Source**: https://docs.anthropic.com/en/docs/claude-code/setup

This token is obtained by running the `claude` CLI's own OAuth device-code flow. It is not a standard OAuth refresh token bundle (which the `OAuthBackend` can auto-refresh); it is the OAuth setup-token itself. Re-issue requires re-running the `claude` CLI authentication flow. No documented API endpoint for silent renewal outside that flow.

---

### GitHub personal access token / gh CLI OAuth token

**Vault examples**: `github-pat-dostal` (Pantheon vault), `orchestration-dev-gh-token` (ffe-cicd)

**Credential type**: GitHub classic PAT (`ghp_...`) or gh CLI OAuth token (`gho_...`) obtained via `gh auth login` device-code flow.

**Source**: https://docs.github.com/en/authentication/keeping-your-account-and-data-secure/managing-your-personal-access-tokens

Classic GitHub PATs cannot be created via the REST API — creation requires browser-based authentication through GitHub's UI. Fine-grained PATs can be revoked via API but not created via API. The `gho_...` OAuth token produced by `gh auth login` requires re-running the device-code consent flow to replace. No silent, programmatic re-issue path exists for either type.

Note: GitHub App installation tokens rotate automatically (1-hour TTL) and are not in scope here — none of the stored names indicate an installation token.

**Update 2026-10-16 (PANT-1141):** an OAuth App token issued by an OAuth App *the operator owns* can be reset via `PATCH /applications/{client_id}/token`. `GitHubRotationAdapter` now does this. A `gh auth login` token (like `github-pat-dostal`) is issued by GitHub CLI's own OAuth App, so it is still manual. See the per-subtype breakdown in [rotation.md](rotation.md#github-per-credential-subtype).

---

### Stripe standard API key (secret and publishable)

**Vault examples**: `stripe-dev-secret-key`, `stripe-prod-secret-key`, `stripe-dev-publishable-key`, `stripe-prod-publishable-key`, `game-library-dev-stripe-secret-key`, `venues-dev-secret-stripe-api-key`, `venues-dev-next-public-stripe-api-key`, `game-library-dev-react-app-stripe-publishable-key`

**Credential type**: Stripe standard secret API key (`sk_live_...` / `sk_test_...`) and publishable key (`pk_live_...`). Based on naming convention; stored as standard keys.

**Source**: https://stripe.com/docs/keys#rolling-keys

Stripe's Dashboard provides a "Roll API key" action for standard keys. No Stripe REST API endpoint exists for creating new standard API keys or rolling existing ones programmatically.

**Stripe restricted keys** (`rk_...`) are a different credential type with a documented API for creation and deletion (`POST /v1/restricted_keys`, `DELETE /v1/restricted_keys/{id}`). None of the stored keys appear to be restricted keys based on naming convention. If any are confirmed to be restricted keys, the finding for that credential changes to `auto`.

---

### Slack incoming webhook URL

**Vault examples**: `event-api-dev-slack-webhook-url`, `event-api-prod-slack-webhook-url`, `orchestration-dev-slack-app-token`, `monitoring-dev-slack-webhook-url`

**Credential type**: Slack incoming webhook URL (a static `hooks.slack.com` URL tied to a specific channel) and Slack app-level token (`xapp-...`).

**Source**: https://api.slack.com/messaging/webhooks

Incoming webhook URLs are generated when an app configures a webhook for a workspace channel. Creating a replacement webhook requires adding a new incoming webhook via the Slack App configuration UI — not a REST API call. `xapp-...` Socket Mode app tokens are similarly managed through the Slack app settings UI.

---

### Meetup API key and OAuth app credentials

**Vault examples**: `event-api-dev-meetup-key`, `event-api-prod-meetup-key`, `event-api-dev-meetup-client-id`, `event-api-dev-meetup-client-secret`, `event-api-prod-meetup-client-id`, `event-api-prod-meetup-client-secret`

**Credential type**: Meetup personal API key (legacy) and OAuth 2.0 application client credentials (client_id + client_secret). Both types stored.

**Source**: https://www.meetup.com/api/

Personal API keys are managed in Meetup account settings. OAuth application client credentials are issued and managed through the Meetup developer settings UI. No Meetup API endpoint for programmatic management of OAuth app registrations (client_id/secret) or personal API keys.

**Update 2026-10-17 (PANT-1126)**: Meetup retired API-key authentication on 2019-08-15; OAuth 2.0 is the only auth method, and creating OAuth consumers needs an active Meetup Pro subscription (https://help.meetup.com/hc/en-us/articles/41467209211917). The v3 REST endpoints that accepted `?key=` now return 404. A stored `*-meetup-key` therefore cannot authenticate anything. Confirm `event-api` no longer reads it, then drop it rather than re-issuing it. Re-issue runbook: [rotation.md, Meetup](rotation.md#meetup-oauth-client-and-retired-api-key).

---

### Eventbrite personal token and OAuth app credentials

**Vault examples**: `event-api-dev-eventbrite-api-key`, `event-api-prod-eventbrite-api-key`, `event-api-dev-eventbrite-private-token`, `event-api-prod-eventbrite-private-token`, `event-api-*-eventbrite-oauth-client-id`, `event-api-*-eventbrite-oauth-client-secret`

**Credential type**: Eventbrite personal OAuth token (`private_token`) / API key AND OAuth application client credentials (client_id + client_secret). Multiple credential types stored under this provider.

**Source**: https://www.eventbrite.com/platform/docs/authentication

Eventbrite's personal API tokens (including `private_token`) and OAuth app credentials are managed exclusively through the Eventbrite developer portal. No Eventbrite API endpoint for programmatic issuance or rotation of personal tokens or OAuth app client credentials.

Re-issue runbook: [rotation.md, Eventbrite](rotation.md#eventbrite-private-token-and-oauth-app-credentials).

---

### Ticketmaster consumer key and consumer secret

**Vault examples**: `event-api-*-ticketmaster-consumer-key`, `event-api-*-ticketmaster-consumer-secret` (`*-ticketmaster-callback-url` is config)

**Credential type**: Ticketmaster Developer app credentials. The consumer key is the `apikey` query parameter on the Discovery API. The consumer secret is issued with it.

**Source**: https://developer.ticketmaster.com/products-and-docs/apis/getting-started/

**Researched 2026-10-17 (PANT-1126)**: keys are issued per app on the developer portal (developer-account.ticketmaster.com > *My Apps*). Ticketmaster's developer docs document no endpoint to create, regenerate or revoke an app's keys. The API gateway is Apigee (an invalid key gets `401 {"fault":{"faultstring":"Invalid ApiKey","detail":{"errorcode":"oauth.v2.InvalidApiKey"}}}`, observed 2026-10-17). Apigee's key-pair APIs (`organizations.developers.apps.keys`) are management-plane APIs for the Apigee org owner, which is Ticketmaster, so app developers can't call them. Classified `manual`: this is a documented absence, not a provider statement that rotation is impossible. Reclassify if Ticketmaster publishes a developer key API. Runbook: [rotation.md, Ticketmaster](rotation.md#ticketmaster-consumer-key-and-secret).

---

### SeatGeek client id and client secret

**Vault examples**: `event-api-*-seatgeek-client-id`, `event-api-*-seatgeek-client-secret`

**Credential type**: SeatGeek Platform API client credentials. `client_id` is sent as a query parameter (or the HTTP Basic username). `client_secret` is optional on public reads.

**Source**: https://developer.seatgeek.com/ (the API's own error points to the management page: `403 "Client is required - visit https://seatgeek.com/account/develop"`, observed 2026-10-17)

**Researched 2026-10-17 (PANT-1126)**: clients are created and managed on the SeatGeek account's *Develop* page (seatgeek.com/account/develop). The Platform API (`api.seatgeek.com/2`) has read endpoints only (events, performers, venues, taxonomies, recommendations), with no client-management endpoint. An invalid `client_id` gets `403 "Invalid client credentials"` (code 40302). Classified `manual`. **Not verified** (needs a logged-in account): whether the Develop page can regenerate a secret in place or only create a new client. Runbook: [rotation.md, SeatGeek](rotation.md#seatgeek-client-credentials).

---

### Apple Push Notification Service (APNS) auth key

**Vault examples**: `apns-authkey-B9FVCQ4GB4`, `apns-authkey-U22RLFH997`, `apns-authkey-unknown-jan9`, `shindig-apns-authkey-p8`

**Credential type**: APNS token-based auth key (`.p8` file, 10-character key ID), issued through Apple Developer Portal.

**Source**: https://developer.apple.com/documentation/usernotifications/setting_up_a_remote_notification_server/establishing_a_token-based_connection_to_apns

Apple provides no public API for programmatic issuance or revocation of APNS auth keys. All management is through the Apple Developer Portal (Certificates, Identifiers & Profiles).

---

### Apple App Store Connect API key

**Vault examples**: `orchestration-prod-apple-asc-api-key-p8`, `shindig-prod-asc-key-base64`, `shindig-prod-asc-key-id`, `shindig-prod-asc-issuer-id`

**Credential type**: App Store Connect API key (private key in `.p8` format, with associated key ID and issuer ID), issued through App Store Connect.

**Source**: https://developer.apple.com/documentation/appstoreconnectapi/creating_api_keys_for_app_store_connect_api

App Store Connect API keys are issued through the App Store Connect web UI. The App Store Connect API authenticates using these keys but provides no endpoint for issuing or revoking them.

---

### Apple code-signing certificate

**Vault examples**: `apple-developer-cert-cer`, `shindig-ios-cert-43F7V53843-cer`, `shindig-ios-cert-43F7V53843-p12`, `shindig-ios-cert-9JYA8DSMY6-cer`, `shindig-ios-cert-9JYA8DSMY6-p12`, `shindig-distribution-cert`, `shindig-appstore-profile`

**Credential type**: Apple developer and distribution code-signing certificate (X.509, `.cer` / `.p12`).

**Source**: https://developer.apple.com/support/certificates/

Code-signing certificates are issued by Apple's Certificate Authority through the Developer Portal or Xcode. No public API for programmatic issuance or revocation.

---

### Android signing keystore

**Vault examples**: `shindig-android-keystore-base64`, `shindig-android-keystore-password`, `shindig-dev-keystore-base64`, `shindig-dev-keystore-password`

**Credential type**: Android Java Keystore file (`.jks` / `.keystore`) containing the signing private key, plus associated passwords.

**Source**: https://developer.android.com/studio/publish/app-signing

Android app signing keystores are locally generated artifacts, not issued by a third-party service. Rotation requires generating a new keystore and updating the Google Play Developer account's upload-key registration — a process that requires explicit Google Play support action once an app is live. No programmatic rotation path.

---

### Grafana self-hosted admin password

**Vault examples**: `monitoring-dev-grafana-admin-password`, `monitoring-dev-grafana-root-url`

**Credential type**: Grafana instance admin password (a configuration value set in `grafana.ini`, not a cloud-managed token).

**Source**: https://grafana.com/docs/grafana/latest/administration/user-management/server-user-management/

Changing the admin password requires authenticating with the current password to call `PUT /api/admin/users/{id}/password` — the API call itself requires the existing credential, making this an interactive/manual operation. Not autonomously rotatable.

---

### n8n encryption key and basic-auth password

**Vault examples**: `social-engine-dev-n8n-encryption-key`, `social-engine-dev-n8n-basic-auth-password`, `social-engine-dev-n8n-basic-auth-user`

**Credential type**: Self-hosted n8n instance configuration values — an AES encryption key protecting stored workflow credentials, and HTTP basic-auth credentials for accessing the n8n UI.

**Source**: https://docs.n8n.io/hosting/configuration/environment-variables/security/

These are startup configuration values, not provider-issued tokens. Rotating the encryption key requires decrypting all stored n8n credentials with the old key and re-encrypting with the new one before restart. No automated rotation path.

---

### JWT secret / symmetric signing key

**Vault examples**: `event-api-dev-jwt-secret`, `event-api-prod-jwt-secret`, `social-engine-dev-cron-secret`, `social-engine-dev-oauth-token-encryption-key`, `social-engine-prod-oauth-token-encryption-key`, `social-engine-dev-byok-master-key`

**Credential type**: Application-managed symmetric secret key for JWT signing or symmetric encryption (not issued by a third-party provider).

These are application-level secrets with no provider API. Rotation requires updating the value in all consumers simultaneously (coordinated deployment). No external API governs them.

---

## `unknown` — rotation story not yet established from provider's own docs

`unknown` is the honest default; it does not mean `manual`. Each row below lists what would be needed to resolve the finding.

| provider | credential_type | vault_examples | what to verify |
|---|---|---|---|
| Autumn | App secret key / webhook secret | `autumn-sandbox-flayr-secret-key`, `*-autumn-secret-key`, `*-autumn-webhook-secret` | Autumn (useautumn.com) subscription billing; no rotation API found in public docs at research time |
| Browserbase | API key | `browserbase-api-key`, `browserbase-project-id` | Browser automation SaaS; `browserbase-project-id` is a config value, not a credential; key rotation API not found |
| Clerk | Backend secret key / webhook secret | `*-clerk-secret-key`, `*-clerk-webhook-secret`, `*-clerk-publishable-key` | Clerk Dashboard has a "Rotate API key" UI; whether there is a REST API path for rotation was not confirmed from docs |
| Convex | Deploy key / internal secret | `*-convex-deploy-key`, `*-convex-internal-secret`, `*-convex-deployment` | Convex management API exists; key rotation endpoints for deploy keys not confirmed; `*-convex-deployment` is a URL (config, not credential) |
| Firecrawl | API key | `flayr-firecrawl-api-key`, `*-firecrawl-api-key` | Web-crawling SaaS; key rotation API not found in public docs |
| Fly.io | API token | `monitoring-dev-fly-api-token`, `orchestration-dev-fly-api-token` | Fly.io has `flyctl tokens create` CLI; REST API rotation path not confirmed in official docs at research time |
| Google / Firebase non-SA, non-API-key | `firebase-storage-base`, `firebase-project-id`, `google-cloud-project` | Various | These are configuration values (URLs, project IDs), not credentials. Not rotateable. |
| Grafana Cloud (stack-scoped data source credentials) | Prometheus remote write token, Loki URL + credentials | `monitoring-dev-grafana-cloud-prom-token`, `monitoring-dev-grafana-cloud-prom-username`, `monitoring-dev-grafana-cloud-loki-url` | Different from org-level Grafana Cloud API key (see `auto`). These are stack-instance credentials for data source push endpoints. Rotation via Grafana Cloud stack management API not confirmed. |
| Healthchecks.io | API key / ping key | `*-healthchecks-api-key`, `*-healthchecks-ping-key`, `*-healthchecks-ping-url` | Healthchecks.io REST API (https://healthchecks.io/docs/api/) covers check management; API key rotation specifics not confirmed |
| MongoDB Atlas | Connection URI (contains DB user credentials) | `*-mongo-uri`, `*-mongo-db-name` | MongoDB Atlas Admin API supports programmatic DB user creation/password change (`POST /api/atlas/v2/groups/{groupId}/databaseUsers`), which would allow updating the credentials in the URI. End-to-end rotation confirmed possible in principle; Atlas API management credential setup not verified against this specific vault. `*-mongo-db-name` is a config value, not a credential. |
| Multica | Workspace personal access token | `dostal-tech-multica-pat-token` | No public Multica API documentation for programmatic PAT management confirmed at research time |
| Pexels | API key | `social-engine-dev-pexels-api-key`, `social-engine-prod-pexels-api-key` | Stock image API; key management via developer portal; rotation API not confirmed |
| Pixabay | API key | `social-engine-dev-pixabay-api-key`, `social-engine-prod-pixabay-api-key` | Stock image API; key management via developer portal; rotation API not confirmed |
| PostHog | Project API key / server API key | `*-posthog-api-key`, `*-posthog-server-key`, `*-posthog-key`, `*-posthog-host`, `*-posthog-tracking` | PostHog personal API keys (for personal developer access) ARE manageable via API (`/api/users/@me/personal_api_keys/`). The keys stored here appear to be project API keys (used by applications), which have a different management path. `*-posthog-tracking` and `*-posthog-host` are config values, not credentials. Project API key rotation not confirmed via API. |
| Qdrant Cloud | Cloud API key | `qdrant-cloud-api-key`, `qdrant-cloud-url` | Qdrant Cloud management API exists; API key rotation specifics not confirmed; `qdrant-cloud-url` is a config value. |
| SerpAPI | API key | `*-serpapi-key` | Search API; key management via dashboard; rotation API not confirmed |
| Shippo | API key / webhook secret | `game-library-dev-shippo-api-key`, `game-library-dev-shippo-webhook-secret` | Shipping API; key management endpoints not confirmed |
| Slack OAuth app credentials | Client ID / client secret / signing secret | `monitoring-dev-slack-client-id`, `monitoring-dev-slack-client-secret`, `monitoring-dev-slack-signing-secret`, `monitoring-dev-slack-verification-token`, `monitoring-dev-slack-app-id` | Slack app-level credentials issued per app registration; rotation requires Slack app reconfiguration; programmatic path not confirmed. `monitoring-dev-slack-app-id` is a config value, not a credential. |
| Tremendous | API key / webhook secret | `flayr-tremendous-api-key`, `social-engine-prod-tremendous-webhook-secret` | Rewards platform; key management via dashboard; rotation API not confirmed |
| Upstash | Redis REST token / API key | `*-upstash-redis-rest-token`, `*-upstash-redis-rest-url`, `*-upstash-api-key`, `orchestration-dev-upstash-email` | Upstash management API (`/v2/redis`) exists with a `reset-password` endpoint; whether that covers the REST access token specifically is not confirmed. `*-upstash-redis-rest-url` is a config value. `orchestration-dev-upstash-email` is an account identifier, not a credential. |
| Vercel AI | API key | `game-library-dev-vercel-ai-api-key` | Vercel AI is a distinct product from Vercel CI/CD (see `vercel-deploy-token` in `auto`); Vercel AI API key rotation endpoints not confirmed separately |
| Zernio | API key / webhook secret | `social-engine-*-zernio-api-key`, `social-engine-*-zernio-webhook-secret` | Communications API; no rotation API found in public docs |

---

## Credential types that are not rotateable (config / IDs)

The following secret names in `ffe-cicd` store configuration values (URLs, IDs, boolean flags, project names) rather than credentials. Rotation is not applicable.

- `*-node-env`, `*-app-url`, `*-port`, `*-log-level` — runtime configuration
- `*-firebase-project-id`, `*-firebase-storage-base`, `*-google-cloud-project` — GCP project identifiers
- `*-mongo-db-name` — database name
- `*-convex-deployment`, `*-convex-url` — Convex deployment URL
- `*-posthog-host`, `*-posthog-tracking`, `*-posthog-host` — PostHog host configuration
- `*-grafana-cloud-org-id`, `*-grafana-cloud-stack-id`, `*-grafana-cloud-stack-url`, `*-grafana-cloud-loki-url`, `*-grafana-cloud-prom-url`, `*-grafana-cloud-prom-username`, `*-grafana-cloud-tempo-url` — Grafana stack identifiers and URLs
- `*-upstash-redis-rest-url`, `*-upstash-email` — connection URL and account identifier
- `*-qdrant-cloud-url` — Qdrant cluster URL
- `monitoring-dev-otel-*` — OpenTelemetry export configuration
- `*-slack-app-id`, `*-discord-*` — app identifiers
- `ffe-orchestration-dev-youtrack-base-url` — YouTrack instance URL
- `*-google-play-package-name`, `*-android-sha256-fingerprint` — app identifiers
- `game-library-dev-tabletop-bot-email`, `game-library-dev-tabletop-support-email` — email addresses
- `shindig-mts-test-email` — test user identifier
- `opus-clip-affiliate` — affiliate code
- `*-enable-*`, `*-ai-gen-*` — feature flags

---

## Not in this vault (notable omissions)

- **AWS credentials**: No AWS access keys, secret keys, or session tokens. `AWSSecretsManagerBackend` is a stub (backend.py); no AWS-backed references exist.
- **HashiCorp Vault / Infisical / Doppler / 1Password / Azure Key Vault tokens**: All stub backends (backend.py); no references backed by these providers.
- **Stripe restricted API keys** (`rk_...`): Not observed; if present, rotation capability would be `auto`.
- **OAuth refresh token bundles** (`oauth:*` namespace): Not stored via `portunus oauth store`; the `OAuthBackend` machinery exists (auth.py, backend.py) but is unused in this vault.
