# Design discussion: portunus-oauth-token-broker

## 0. Goal

If built: let Portunus hold a provider-issued OAuth refresh token (obtained by the user through
their own one-time, provider-legitimate consent flow) and mint short-lived access tokens from it
on demand, injected only at the execution boundary — the same safety shape every other secret
already gets, generalized to a token type that expires and must be refreshed rather than a
static value.

## 1. Three shapes considered

**A. Portunus runs the OAuth consent flow itself** (registers its own OAuth client per provider,
opens a browser, runs a local loopback redirect handler, captures the resulting refresh token).
This is what `gcloud`/`gh`/`rclone` all do for their own scopes. Cost: real engineering (a local
HTTP server for the redirect, PKCE handling, per-provider client registration) *plus*, for any
Google sensitive/restricted scope, the full Google verification/CASA burden research-brief.md §4
describes — a cost that doesn't shrink no matter how well this is engineered, since it's Google's
gate, not Portunus's. For non-Google providers with lighter verification models, engineering-only
cost is more reasonable, but building a bespoke consent-flow runner *for Portunus's own OAuth
client* is still a meaningfully larger, higher-blast-radius surface than the alternative below.

**B. Portunus is storage + minting only; the user bootstraps the refresh token themselves.** The
one-time consent flow happens OUTSIDE Portunus, through whatever mechanism already legitimately
covers the target scope (`gcloud auth application-default login --scopes=...` for GCP-adjacent
access — already how `portunus auth login` works today; a provider's own CLI/OAuth playground for
others). The user hands Portunus the resulting `(client_id, client_secret, refresh_token,
token_endpoint, scope)` bundle once; Portunus's job starts there. This reuses
`LocalEncryptedBackend.store_session()`'s exact existing shape (research-brief.md §3) and adds
only the refresh-mint step.

**C. Don't build anything — tell users to run their own token-refresh script per provider.**
Zero Portunus engineering cost, but no shared boundary-safety discipline, no audit trail, no
single place a human manages these credentials alongside everything else in the vault — every
provider's refresh logic gets hand-rolled per project, the exact fragmentation Portunus exists to
prevent for static secrets.

**Recommendation: B.** It's the smallest real addition (research-brief.md §3: storage and
boundary-safe retrieval already exist; only the mint step is new), it doesn't take on Google's
consent-flow verification burden as Portunus's own problem, and it matches this codebase's
already-established, already-working precedent (`portunus auth login` delegating the actual OAuth
dance to `gcloud`, never reimplementing it) rather than introducing a new posture.

## 2. Shape B in concrete terms

**Storage.** Reuse `store_session()`/`load_session()` directly — an OAuth credential bundle is
already exactly what that function stores (an arbitrary JSON-serializable payload, TTL-aware,
namespaced by site/account). No new storage primitive needed.

**Self-grill: reuse the `session:` namespace as-is, or add a distinct `oauth:` one?** A distinct
prefix (e.g. `oauth:<provider>:<account>`) is worth the small cost — `list_sessions()`/
`cmd_session_list` currently present everything under one undifferentiated "sessions" label; a
human scanning `portunus session list` shouldn't have to guess whether an entry is a Playwright
`storageState` or an OAuth credential bundle. This is a few lines (a second `key_prefix` param
threaded through the existing methods, or a thin sibling method) — not a new storage engine.

**Minting.** A new `GoogleOAuthRefresh`-shaped class (or provider-generic
`OAuthRefreshTokenAuth`, parameterized by `token_endpoint`/`client_id`/`client_secret`), mirroring
`GCPWorkloadIdentityAuth.mint()`'s exact existing shape in `auth.py`: POST
`grant_type=refresh_token` to the provider's token endpoint, parse `access_token`/`expires_in`
from the response, return a short-lived token object. Same `transport` dependency-injection
pattern the existing WIF classes already use (real HTTP by default, a fake in tests) — no new
testing pattern to invent.

**Boundary injection.** A new `SecretBackend`-shaped adapter (call it `OAuthBackend`, alongside
`GcloudBackend`/`AWSSecretsManagerBackend` in backend.py) whose `.access(sm_name, project="")`
loads the stored refresh bundle, mints a fresh access token (or reuses a cached one if not yet
expired — mirroring `GCPWorkloadIdentityAuth`'s own `expired()` skew logic), and returns the
access token string. From the `Resolver`'s point of view this is just another backend — a
reference with `backend="oauth"` (or a project's `VaultBinding.backend="oauth"`) routes through
it via the existing `_make_backend_router()` precedence, completely unchanged. `{{secret:my-
gmail-token}}` then resolves to a live, auto-refreshed access token through the exact same
`resolve`/`resolve_exec`/`resolve_to_tempfile` boundary paths every other secret already uses —
zero new boundary-safety code, because the existing one already covers this once the value
*is* a string, which an access token is.

**Bootstrap (out of Portunus's own scope).** A new `portunus oauth store <provider> <account>`
command (mirroring `session store`'s exact CLI shape: `--stdin`/`--value-file`, never inline
argv) that takes the JSON bundle the user obtained via their own one-time consent flow and calls
the (renamed/generalized) storage method. The command's own help text and README documentation
point at the provider-appropriate bootstrap mechanism (`gcloud auth application-default login
--scopes=...` for GCP-adjacent access today; documented guidance for other providers as they're
added) — Portunus never runs a browser redirect itself.

## 3. Self-grill

**Does this reintroduce the "capture a session" risk the original ask was steered away from?**
No — an OAuth access token is scoped (specific permissions, not full-account), short-lived
(minutes to an hour, not an indefinite browser session), and revocable independently of the
account's actual login session. It's the same trust shape as every other secret Portunus already
brokers (a GCP access token minted via WIF is exactly this same shape) — not a new risk class.

**What happens when the refresh token itself expires or is revoked (e.g. the 7-day Testing-mode
expiry for an unverified Google app)?** `OAuthBackend.access()` raises `BackendError` on a failed
refresh grant, exactly like `GcloudBackend`'s existing IAM-failure path — `portunus vault access
verify` (portunus-vault-transfer, already shipped) already translates a `BackendError` into an
actionable hint; the same mechanism applies here with a hint pointing back at the bootstrap
command. No new failure-handling pattern needed, the existing one already generalizes.

**Should minted access tokens be cached, or re-minted on every resolve?** Cache with the same
expiry-skew check `GCPWorkloadIdentityAuth`/`GCPAccessToken` already use (`expired(skew=30)`) —
re-minting on every single resolve when many calls happen in a short window would be wasteful and
puts unnecessary load on the provider's token endpoint. Cache lives in memory for the process
lifetime only (never written to disk unencrypted) — mirrors how `SyncingBackend`'s cached mode
already treats "don't hit the network more than needed" without ever persisting anything beyond
what's already encrypted at rest.

**Does this need a UI surface (Standalone UI) in v1?** No — `portunus session store/list` already
has no UI surface either (CLI-only, matching the "a human directly runs this bootstrap step"
posture), and an OAuth credential bundle is exactly as sensitive as a session, so the same
CLI-only precedent applies without needing a fresh justification.

**Version bump if built:** minor — new backend kind, new CLI command, additive; nothing existing
changes shape.

## 4. What this analysis is NOT recommending

Not recommending: Portunus running any OAuth consent/redirect flow itself (shape A); anything
that touches a live browser session/cookie (research-brief.md §2); solving Google's
verification/CASA requirement (not solvable by engineering — see cost-benefit-analysis.md).
