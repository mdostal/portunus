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
value, for providers that use something like `X-Api-Key`), `query_param`
(send the value as that URL query parameter instead of a header; the result's
`detail` shows only the base URL) and `dead_statuses`.

When a provider issues several credentials and only some can be sent on
their own (a token can, its OAuth client secret can't), use
`CredentialKindLivenessAdapter`. It probes only the kinds listed in
`probe_kinds`. Every other kind reads `unknown` with a reason and no
request, and `liveness check --all` skips it. The kind comes from the
reference's `liveness_kind` tag, or else from the reference name's suffix
(`event-api-prod-eventbrite-private-token` -> `private-token`). Use the provider's cheapest endpoint that needs
authentication and never returns business data. Confirm it against the
provider's API docs and a real key.

### Registered providers

| Provider | Probe | Dead | Notes |
|---|---|---|---|
| `github` | `GET https://api.github.com/rate_limit` (Bearer) | 401 only | Doesn't count against the rate limit. GitHub's 403 can mean rate-limited, so it reads `unknown`. GitHub App private keys aren't bearer tokens: they read `unknown` without a request. A stored `ghs_` installation token reads `dead` after its 1-hour expiry. See [rotation.md](rotation.md#github-per-credential-subtype). |
| `eventbrite` | `GET https://www.eventbriteapi.com/v3/users/me/` (Bearer) | 401 only | Probes `*-private-token` only. The app's API (client) key and OAuth client id/secret are not bearer tokens: `unknown`, no request. Tag `liveness_kind=private-token` on an `api-key` reference that is really a personal token. |
| `ticketmaster` | `GET https://app.ticketmaster.com/discovery/v2/classifications.json?size=1&apikey=<key>` | 401 only | Probes `*-consumer-key` only. 403 can mean a working key without the Discovery product, so it's `unknown`. The consumer secret is never sent: `unknown`. |
| `seatgeek` | `GET https://api.seatgeek.com/2/taxonomies?client_id=<id>` | 401, 403 | Probes `*-client-id` only. An invalid id gets 403 `Invalid client credentials` (code 40302). The secret is only valid next to its id: `unknown`. |
| `meetup` | none | n/a | Every Meetup reference reads `unknown` with the reason, and sweeps skip them. API-key auth was retired on 2019-08-15. The GraphQL API answers a bad token with HTTP 200 (error only in the body, which liveness never reads). Client credentials only work inside an OAuth exchange. |

The other provider sub-issues of #141 add their own: Vercel (#144), Autumn (#143), Clerk (#145)
and Convex (#142).

The event-source probes (PANT-1126 / #203) pick their adapter from `provider`, so they only
apply once the references carry their real provider rather than `gcp`. See
`scripts/backfill_event_api_providers.py` and [rotation.md](rotation.md#event-source-apis-used-by-event-api-meetup-eventbrite-ticketmaster-seatgeek).

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
