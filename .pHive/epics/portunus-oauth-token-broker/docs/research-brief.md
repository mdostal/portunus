# Research brief: portunus-oauth-token-broker

## 1. The user's own framing

> "can we also capture google auth logins and things like that so you can log in your gmail
> session, share that across, use it for oauth, sso, etc? if not, can we do a deep dive plan
> and CBA around the possibility of that?"

This is a deep-dive research + design + cost-benefit pass, per the user's own explicit request
after I recommended against literal Gmail-session capture in favor of proper OAuth token
brokering. **This document set is analysis, not a commitment to build** — no `epic.yaml`/story
YAMLs are written here; the closing recommendation in `cost-benefit-analysis.md` is a decision
point for the user, matching how every prior epic this session that started from a genuinely
open product question (not a firm directive) has been handled.

## 2. What "capture a Gmail session" would actually mean, and why it's the wrong shape

A browser session cookie is a **continuously-used, full-account-access bearer credential** tied
to device/IP/browser fingerprints. Unlike an API key (used once per call, revocable, scoped),
a session cookie:
- Is used by a live browser on every request, not injected once at an execution boundary —
  there is no single boundary call site to inject it at, which breaks Portunus's core
  boundary-only-injection invariant structurally, not just as an implementation gap.
- Triggers Google's own anti-abuse systems when reused from a new device/location/IP — "sharing
  it across" machines/agents is the exact behavior those systems exist to catch, so this would
  likely get flagged, force a re-login, or (worse) get the account locked, not work reliably.
- Grants full account access (read/send/delete email, contacts, etc.), a far broader blast
  radius than a scoped OAuth token with specific scopes.

**Conclusion, unchanged from the earlier recommendation:** don't build session-cookie capture.
The legitimate version of "log in once, reuse the identity elsewhere" is OAuth 2.0 — get a
scoped, revocable, provider-managed refresh token through a real consent flow once, then mint
short-lived access tokens from it on demand. That's what this research targets instead.

## 3. What Portunus already has — confirmed by reading the actual code, not assumed

**`src/portunus/auth.py` — keyless workload identity federation, not personal OAuth login.**
`GCPWorkloadIdentityAuth`/`AWSWebIdentityAuth` exchange a short-lived, harness-provided OIDC
token (e.g. from GitHub Actions) for short-lived cloud credentials via GCP's Security Token
Service / AWS STS `AssumeRoleWithWebIdentity`. **No refresh token is ever stored** — the whole
point of this module is to never hold a long-lived credential at all. This is a different
problem from "a human logs in once as themselves and Portunus remembers it" — it's for
CI/agent workloads that already have an external trust root, not for a personal Google login.

**`portunus auth login <email>` (cli.py `cmd_auth_login`) — a thin wrapper, not an OAuth
implementation.** It shells out to `gcloud auth login <email>` and nothing else. `gcloud` runs
the entire OAuth consent flow (its own long-since-Google-verified OAuth client, its own browser
redirect, its own credential store under `~/.config/gcloud/`) — Portunus never touches the
resulting tokens. `cmd_auth_status` only cross-references `gcloud auth list`'s account emails
against configured `VaultBinding`s; still no token handling. `GcloudBackend` (backend.py) passes
`--account=<email>` to `gcloud secrets ...` calls, relying entirely on gcloud's own ambient
credential — this is the existing, working precedent for "delegate the actual OAuth dance to the
provider's own official, already-verified CLI wherever one exists," established well before this
research, not invented for it.

**`LocalEncryptedBackend.store_session()`/`load_session()`/`list_sessions()`/`remove_session()`
(localvault.py, wired to CLI via `portunus session store/load/inspect/list/remove`) — this is
the closest existing primitive, and it is almost exactly the shape an OAuth credential needs
already.** It stores an arbitrary JSON-serializable payload, encrypted, under a
`session:<site>:<account>` namespace, with TTL and rotation-generation metadata already built
in. Retrieval (`cmd_session_load`) is **already boundary-safe** — a 0600 tempfile, path printed,
never the payload itself, identical discipline to `resolve`. This was built for Playwright-style
browser automation state, but structurally it's "store a JSON credential bundle, retrieve it
safely" — exactly what an OAuth refresh-token bundle (`client_id`, `client_secret`,
`refresh_token`, `token_endpoint`, `scope`) needs. **This is the single biggest finding of this
research**: the storage + safe-retrieval half of an OAuth broker is not a gap to build — it
already exists, in production, tested.

**What's genuinely missing:** a "mint a short-lived access token from a stored refresh token"
step (nothing like this exists yet for anything except the OIDC-workload-identity path in
`auth.py`, which is a different flow entirely — token exchange, not token refresh), and wiring
that mint step into the boundary-injection path so `{{secret:some-oauth-ref}}` resolves to a
live, auto-refreshed access token rather than a raw stored blob.

## 4. What Google actually requires for this — verified via current search, not assumed stale

Google's OAuth requirements are the dominant real cost here, and they don't move regardless of
how well Portunus's own engineering is done:

- **A personal `@gmail.com` account cannot use the "Internal" consent-screen audience** — that
  option requires a Google Workspace organization. A personal account is stuck with "Testing" or
  "External."
- **"Testing" status**: refresh tokens issued expire after **exactly 7 days**, unconditionally,
  unless the only scopes requested are `openid`/`userinfo.email`/`userinfo.profile`. Any real
  Gmail/Workspace API scope forces this 7-day expiry in Testing mode. Re-consent every week is
  the practical reality for an unverified personal-use app with real scopes.
- **"External" + full verification**: required to get an indefinite-lifetime refresh token.
  Sensitive scopes need standard Google app verification (2–6 weeks turnaround); **restricted**
  scopes (full Gmail read/send access is squarely in this tier) additionally require a **CASA
  Tier 2 security assessment** — a real, recurring, non-engineering administrative burden
  (privacy policy, domain ownership verification, security questionnaire), not a one-time cost,
  and completed verification itself has its own re-verification lapse window.
- **Checked and ruled out as a shortcut**: using `gcloud`'s own already-verified OAuth client via
  `gcloud auth application-default login --scopes=<gmail-scope>` (a real, documented flag) does
  **not** bypass this — Google's verification status is scoped to the *(client, requested-scope)*
  pair, not the client alone, so requesting an out-of-scope sensitive/restricted scope through
  gcloud's client still surfaces the "unverified app" treatment for that grant. It also stretches
  gcloud's own API Terms of Service (its client is declared/verified for Google Cloud Platform
  API access, not as a general-purpose Gmail OAuth proxy) — not a workaround worth relying on or
  recommending.

**What this does NOT block**: OAuth for scopes that are *not* sensitive/restricted (most non-
Google providers' APIs — GitHub, Slack, generic Microsoft Graph scopes, etc. — have materially
lighter verification models), and GCP-resource-scoped access, which `gcloud`'s already-verified
client already handles today via the existing `portunus auth login` wrapper. The friction is
specific to Google's *sensitive/restricted* scope tiers (Gmail chief among them), not to OAuth
brokering in general.

## 5. Scope boundary for this analysis

In scope for the design/CBA that follow: a **generic, provider-agnostic** OAuth 2.0
refresh-token storage + access-token minting mechanism, reusing the existing session-store
primitive and mirroring `GCPWorkloadIdentityAuth`'s existing `mint()` shape. Out of scope,
explicitly: implementing Google's OAuth *consent* flow inside Portunus itself (the bootstrap
step — obtaining the first refresh token — is the user's own one-time action, using whatever
provider-appropriate, already-legitimate mechanism fits: `gcloud` for GCP-scoped access, a
provider's own CLI/OAuth playground for others); anything that resembles session-cookie capture
(§2); Google-specific verification/CASA work (not an engineering task Portunus can do on the
user's behalf).

## Sources

- [Google OAuth Refresh Token: Expiration, 7-Day Limit & Lifetime Explained](https://www.unipile.com/google-oauth-refresh-token/)
- [Restricted scope verification — Google for Developers](https://developers.google.com/identity/protocols/oauth2/production-readiness/restricted-scope-verification)
- [Manage App Audience — Google Cloud Platform Console Help](https://support.google.com/cloud/answer/15549945?hl=en)
- [gcloud auth application-default login — Google Cloud SDK docs](https://docs.cloud.google.com/sdk/gcloud/reference/auth/application-default/login)
