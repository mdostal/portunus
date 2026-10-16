# Liveness & Parity

Rotation answers "can Portunus mint a new credential?". Liveness answers the
question that went unanswered in the 2026-09-13 incident (GH #141): **does
the stored copy still work at all?** A dead Vercel deploy token and an
invalid Autumn key sat in GCP Secret Manager for months because nothing ever
exercised them.

## Liveness checks

A liveness adapter makes one cheap, authenticated, read-only call against
the provider's own API and classifies the HTTP status:

| Verdict | Meaning |
|---|---|
| `live` | any 2xx |
| `dead` | the provider rejected the credential (by default 401 or 403) |
| `unknown` | no adapter, reference not resolvable (gated, locked, missing value), network error, or any other status (429, 5xx, ...) |

An outage or rate limit is never reported as `dead`.

The value is resolved through the normal boundary-only resolver and goes
only into the outbound request header. The response body is never read, so
a provider error that echoes the key back cannot reach a result, a log line
or the audit log. Gated and locked references are not probed: the broker's
policy check runs first and they come back `unknown`.

```sh
portunus liveness check <name> [<name> ...] [--json]
portunus liveness check --all [--json]
portunus liveness status [--failing] [--json]
```

`check` exits `0` when every probed reference is live, `2` if any is dead and
`1` if any check could not reach a verdict, so a scheduler can alert on the
exit code alone. `--all` probes every reference whose provider has a
liveness adapter and that is not dropped or revoked.

The adapter is picked from the reference's `provider` field. Set the
`liveness_provider` tag to override it, for example for an Autumn key
registered under provider `gcp` (`retag --tags` replaces the whole tags dict, so
include any existing tags):

```sh
portunus retag social-engine-prod-autumn-secret-key --tags liveness_provider=autumn
```

### The escalation clock

Every check is appended to the audit log (`liveness`, result `live:200`,
`dead:401`, ...) and recorded in `PORTUNUS_HOME/liveness-status.json` (0600).
A reference that fails keeps its `first_failed_at` and a
`consecutive_failures` count until a live check clears them, so
`portunus liveness status --failing` lists dead keys oldest first, with how
long each has been failing.

MCP equivalents: `portunus_liveness_check(names?, all?)` and
`portunus_liveness_status(failing?)`.

### Adding a provider

Most providers only need an `HTTPProbe`:

```python
from portunus.liveness import HTTPLivenessAdapter, HTTPProbe, register_liveness_adapter

register_liveness_adapter("example", lambda: HTTPLivenessAdapter(
    "example", HTTPProbe(url="https://api.example.com/v1/me")))
```

`HTTPProbe` takes `method`, `header` and `scheme` (`scheme=""` sends the raw
value, for providers that use something like `X-Api-Key`) and
`dead_statuses`. Use the provider's cheapest endpoint that needs
authentication and never returns business data. Confirm it against the
provider's API docs and a real key.

### Registered providers

| Provider | Probe | Dead | Notes |
|---|---|---|---|
| `github` | `GET https://api.github.com/rate_limit` (Bearer) | 401 only | Doesn't count against the rate limit. GitHub's 403 can mean rate-limited, so it reads `unknown`. GitHub App private keys aren't bearer tokens: they read `unknown` without a request. A stored `ghs_` installation token reads `dead` after its 1-hour expiry. See [rotation.md](rotation.md#github-per-credential-subtype). |

The other provider sub-issues of #141 add their own: Vercel (#144), Autumn (#143), Clerk (#145)
and Convex (#142).

## Parity

`portunus.liveness.parity_check(ref_name, fetchers)` compares one
reference's value across several targets, such as GCP Secret Manager, the
local vault or a Convex deployment. Each target is a zero-argument fetcher
that returns the target's live value. Use `backend_fetcher(backend, sm_name)`
for any Portunus backend.

Values are compared through an HMAC under a random key generated for that
call alone, and each value is dropped before the next fetch. The result only
contains target names grouped by equal value, plus the exception type of any
target that could not be read. It never contains a value, or a fingerprint
that could be compared across runs:

```python
ParityResult(ref_name="autumn-secret-key",
             groups=(("convex", "vercel"), ("gcp",)),  # gcp has drifted
             errors={})
```

Fetchers for non-Portunus targets (Convex, Vercel) and a CLI surface come
with the Convex sub-issue (#142).
