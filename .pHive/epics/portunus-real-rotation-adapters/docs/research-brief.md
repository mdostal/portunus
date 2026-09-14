# Research brief -- real key rotation across every Portunus-managed credential

## Where this starts from

Two independent pieces already exist in the codebase and have never been connected.

**The shape, with no substance** (`src/portunus/rotation.py`). `RotationBinding{provider,
status, account}` persisted to `PORTUNUS_HOME/rotation-bindings.json` (0600, atomic
replace), `rotation_adapter_for(provider)`, and three adapters -- `vercel`, `github`,
`stripe` -- whose `.rotate()` unconditionally raises `RotationAdapterError` pointing at a
GitHub adapter-request template. The module docstring is explicit that this is deliberate:
"No real provider API is ever called from this module." CLI surface is
`rotation-bindings set/show` (`cli.py:1263`, `cli.py:1278`); MCP surface is
`portunus_rotation_status` (`mcp_server.py:151`), whose docstring still reads "Every
provider is a stub today -- no real rotation has ever fired."

**The substance, not registered as rotation** (`src/portunus/auth.py:286`,
`src/portunus/backend.py:453`). `OAuthRefreshTokenAuth.mint()` runs the RFC 6749 §6
refresh_token grant and returns `OAuthAccessToken` carrying an optional
`rotated_refresh_token`. `OAuthBackend.access()` caches minted tokens in-memory only, and
when the provider rotates the refresh token it immediately writes the new one back via
`local_backend.store_oauth_credential(...)`, best-effort, logging
`credential-mint / ok:refresh-token-rotated` (or `warn:rotation-persist-failed:...`) to the
audit chain rather than failing a mint that already succeeded. The inline comment records
why this is not optional: some providers -- confirmed live for Codex CLI -- invalidate the
spent refresh token, so without the write-back the next mint after the in-memory cache
expires fails against an already-consumed token.

That is a working rotation loop. It just isn't reachable through `rotation_adapter_for()`,
so nothing generic can drive it.

## What story 01 must actually establish

The brief is emphatic that provider rotation stories be **verified, not assumed**. For each
provider actually represented in the vault, record:

| field | requirement |
| --- | --- |
| provider | as keyed in `rotation-bindings.json` |
| credential type | the specific type in the vault (personal API key vs. OAuth app token vs. SA key) -- findings that apply to a different type of the same provider's credentials are not findings |
| capability | `auto` / `manual` / `unknown` -- `unknown` is the honest default |
| source | a URL to the provider's own docs or API reference |
| notes | what the rotation call sequence would be, if `auto` |

Known starting points, all to be confirmed rather than trusted:

- **GCP service-account keys.** Expected `auto`, and the reason this epic exists. IAM
  supports create / disable / delete on keys, which is the full lifecycle needed for a
  safe create -> verify -> retire sequence. Highest generality: applies to every
  GCP-backed project Portunus manages. `GcloudBackend` (`backend.py:205`) already shells
  `gcloud` with per-binding WIF/impersonation, so an adapter has an established auth path
  and does not need a new credential mechanism.
- **Linear API keys.** Several are stored in ffe-cicd (`event-api-dev-linear-api-key`,
  `event-api-prod-linear-api-key`, `orchestration-dev-linear-api-key`). Suspected `manual`
  for *personal* API keys. Do not generalize from any Linear docs page describing OAuth
  application credentials -- that is a different credential type from what is in the vault.
- **OAuth refresh-token-shaped credentials.** Enumerable today via
  `LocalEncryptedBackend.list_oauth_credentials()` (`localvault.py:326`), which returns
  metadata views only and skips undecryptable entries. Anything found here is `auto` by
  construction -- the machinery is already proven.
- **Everything else among the ~343 discovered secrets.** Expected to be mostly `unknown`
  after one research pass. That is a fine outcome; story 03's inventory exists to make the
  unknowns countable rather than invisible.

## The decision that shaped the build stories

The destructive half of GCP SA-key rotation is irreversible and its blast radius is not
knowable from inside Portunus: GCP cannot restore a deleted key, and Portunus has no
inventory of which workloads hold one. So the adapter's **default stops after create and
verify**. `--retire-old` opts into disabling the superseded key, and deletion happens only
after a grace period during which it sits disabled -- a disabled key can be re-enabled, a
deleted one cannot. The cost of this default is that old keys accumulate; story 03's
`rotation audit` is what keeps that visible.

## Boundary note

A rotation adapter is the first thing in this codebase that legitimately holds *newly
created* credential material. The invariant does not change, only its application: new
material goes to the local backend's store and nowhere else. It must not appear in a
`RotationResult`, a CLI print, an MCP response, or an audit entry -- those carry key
identifiers, phase outcomes and timestamps only.
