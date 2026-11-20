# Keyless Claude access (Anthropic Workload Identity Federation)

A Claude workload (Claude Code, or anything built on an Anthropic SDK) can authenticate
without `ANTHROPIC_API_KEY` or `CLAUDE_CODE_OAUTH_TOKEN`. The workload reads a short-lived
OIDC identity token (a JWT) from a file, exchanges it at
`POST https://api.anthropic.com/v1/oauth/token` under a **federation rule**, and gets back a
short-lived Anthropic access token. Nothing long-lived is stored or injected.

Portunus provides the JWT and keeps it fresh. A Google service account signs it, and
Portunus writes it to a 0600 file. Portunus also provides the env vars that point the workload at
that file. Each Anthropic org (for example `mathew.dostal` and `ff.events`) gets its own
**profile**, with its own rule and Anthropic service account. A lane picks its org by running
its agent under `portunus anthropic-federation exec <profile> -- ...`.

```
gcloud / metadata / WIF ──▶ Google ID token (aud=https://api.anthropic.com, ~1h)
        portunus writes it to PORTUNUS_HOME/anthropic-federation/<profile>.jwt (0600)
        and rewrites it at half-life ─────────────────────────────────────┐
claude (ANTHROPIC_FEDERATION_RULE_ID, ANTHROPIC_ORGANIZATION_ID, ...)     │
        reads the file on every exchange ◀───────────────────────────────┘
        └──▶ api.anthropic.com/v1/oauth/token ──▶ sk-ant-oat01-… (short-lived, in memory)
```

## 1. Google side (once per org)

Create one GCP service account per Anthropic org. Its only job is to be the identity in the JWT,
so it needs no GCP roles.

```bash
gcloud iam service-accounts create claude-mathew --project=<gcp-project> \
  --display-name="Claude via Anthropic WIF (mathew)"
SA=claude-mathew@<gcp-project>.iam.gserviceaccount.com
gcloud iam service-accounts describe "$SA" --format='value(uniqueId)'   # numeric `sub`, for the rule
```

Choose where the JWT comes from:

| Source | Where | What it needs |
|---|---|---|
| `gcloud` (default) | a non-GCP host such as dragon | A local gcloud login with `roles/iam.serviceAccountTokenCreator` on `$SA`. A user credential can't mint a token for a custom audience, so Portunus impersonates the service account. |
| `metadata` | GCE, GKE or Cloud Run | The workload's attached service account. Portunus calls the metadata server with `format=full`, so the token carries `email`. |
| `wif` | a host with an OIDC issuer (`PORTUNUS_OIDC_TOKEN[_FILE]`) | The pool principal needs `roles/iam.serviceAccountTokenCreator` on `$SA`. Portunus does the STS exchange, then IAM Credentials `generateIdToken`. |

For `gcloud` on dragon:

```bash
gcloud iam service-accounts add-iam-policy-binding "$SA" \
  --member="user:<you@example.com>" --role=roles/iam.serviceAccountTokenCreator
# check it (the token goes to a file, never the screen):
portunus identity-token mint --audience https://api.anthropic.com \
  --service-account "$SA" --out /tmp/check.jwt && rm /tmp/check.jwt
```

## 2. Anthropic Console (once per org, done by an org admin)

Open **Settings → Workload identity** (`platform.claude.com/settings/workload-identity-federation`)
and choose **Connect workload → Google Cloud**. The wizard creates three things:

1. **Federation issuer** (`fdis_…`): `https://accounts.google.com`, with keys found through
   OIDC discovery.
2. **Service account** (`svac_…`): the Anthropic identity the workload acts as. Add it to the
   workspace the lane should bill, or exchanges fail.
3. **Federation rule** (`fdrl_…`): decides which JWTs may act as that service account. Match all
   three of these:
   - `audience` = `https://api.anthropic.com`
   - `claims.sub` = the numeric `uniqueId` from step 1
   - `claims.email` = `$SA`

   Never match only on a subject prefix. Any Google service account in any project can get a
   token from `accounts.google.com`, so `sub` and `email` are what pin it to yours. The token
   lifetime can be anything from 60 to 86400 seconds. Anthropic also caps each minted token at
   twice the JWT's remaining life.

Note the **organization ID** (a UUID), the rule ID, the service account ID and, if the rule
covers more than one workspace, the **workspace ID** (`wrkspc_…`).

## 3. Portunus profile

```bash
portunus anthropic-federation set mathew \
  --organization-id <org-uuid> --rule-id fdrl_… --service-account-id svac_… \
  --impersonate-service-account "$SA" [--gcloud-account you@example.com] \
  [--workspace-id wrkspc_…] [--note "mathew.dostal"]
portunus anthropic-federation set ff-events --organization-id … --rule-id … \
  --service-account-id … --impersonate-service-account claude-ff@….iam.gserviceaccount.com

portunus anthropic-federation refresh mathew     # mint once into the token file
portunus anthropic-federation verify mathew      # one live exchange; prints expiry/scope only
portunus anthropic-federation status --strict    # conflicts and token freshness
```

`anthropic-federation.json` in the Portunus home holds only IDs and paths. They identify an
org, rule and account; they don't authenticate anyone.

## 4. Running a lane federated

```bash
portunus anthropic-federation exec mathew -- claude -p "…"
```

`exec` does four things:

- Mints the JWT, then starts a background refresher for the life of the child. The refresher
  rewrites the file at half-life, or `refresh_margin` seconds before `exp` if that comes first.
  If the JWT carries `jti` (single-use at Anthropic), it rewrites every 4 minutes.
- Runs the child with the five `ANTHROPIC_*` vars set.
- Removes every credential that outranks federation from the child's env: `ANTHROPIC_API_KEY`,
  `ANTHROPIC_AUTH_TOKEN`, `CLAUDE_CODE_OAUTH_TOKEN`, `ANTHROPIC_PROFILE`,
  `ANTHROPIC_IDENTITY_TOKEN`, and `CLAUDE_CODE_USE_BEDROCK/VERTEX/FOUNDRY`.
- Forwards SIGINT, SIGTERM and SIGHUP to the child and exits with its code.

A failed refresh keeps the old token on disk and retries every 30 seconds.

For a long-running service that manages its own env, run the refresher on its own and source
the env:

```bash
portunus anthropic-federation refresh mathew --watch &      # or a systemd unit
eval "$(portunus anthropic-federation env mathew)"           # unset … / export ANTHROPIC_…
```

`portunus identity-token mint --audience A --out FILE [--watch]` is the generic form, for any
audience and any consumer.

## Precedence pitfalls (why a "federated" run can still bill the wrong account)

Claude Code checks credentials in this order. The first one it finds wins:

1. Cloud provider switches (`CLAUDE_CODE_USE_BEDROCK`, `CLAUDE_CODE_USE_VERTEX`, `CLAUDE_CODE_USE_FOUNDRY`)
2. `ANTHROPIC_AUTH_TOKEN`
3. `ANTHROPIC_API_KEY`
4. `apiKeyHelper`
5. `CLAUDE_CODE_OAUTH_TOKEN`
6. Federation
7. `/login`

- **An empty variable still counts.** `ANTHROPIC_API_KEY=` takes its slot just like a real
  value. `exec` and `env` unset these variables instead of blanking them.
- **`apiKeyHelper`** in `~/.claude/settings.json` or a project `.claude/settings*.json`
  outranks federation. `exec` can't strip it. `status` reports it, and `status --strict` exits 1.
- **An API key saved by `/login`** wins when `CI` is unset. Run `/logout` on that machine, or
  set `CI=1` for the lane.
- **Bare mode, Claude Desktop and cloud sessions** don't read the federation vars.

## Verifying

- In Claude Code, `/status` shows the authentication source. A federated run shows the
  federation profile or rule, not an API key or OAuth token.
- **Console → Workload identity → authentication history** lists each exchange with its rule
  and service account. Anthropic answers every denied exchange with the same opaque `401`, so
  the reason for a failure only appears in that history.
- For a run longer than 1 hour, `portunus anthropic-federation status mathew` shows the token
  file's remaining life going back up after each refresh, and the authentication history keeps
  showing successful exchanges past the first hour.

## Security notes

- The JWT is written only to its 0600 file. The parent directory is 0700 when Portunus
  creates it. The JWT never appears on stdout, in logs or in the audit chain. The audit chain
  records `identity-token-mint` with the service account email and the audience.
- `verify` drops the Anthropic access token as soon as the exchange succeeds and prints only
  `token_type`, `expires_in` and `scope`.
- If the token file leaks, the exposure is at most the JWT's remaining life (about 1 hour) and
  the one federation rule it matches.
