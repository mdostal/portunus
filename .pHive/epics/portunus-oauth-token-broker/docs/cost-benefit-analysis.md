# Cost-benefit analysis: OAuth token brokering in Portunus

## Framing

Three options, evaluated against the same axes: engineering cost, ongoing/non-engineering cost,
security risk, and benefit delivered. "Do nothing" is a real fourth option and is included.

## Option 0 — Do nothing

- **Engineering cost:** zero.
- **Ongoing cost:** zero.
- **Risk:** zero (no new surface).
- **Benefit:** zero — the underlying friction (re-entering tokens, no shared audit trail for
  OAuth-backed access, hand-rolled per-project refresh scripts) stays exactly as it is today.
- **When this is the right call:** if OAuth-backed access isn't actually a recurring pain point
  yet — worth confirming before spending the engineering budget below.

## Option A — Portunus runs its own OAuth consent flow (design-discussion.md §1.A)

- **Engineering cost:** medium-high. A local-loopback redirect handler, PKCE, per-provider OAuth
  client registration and secret management, a new interactive/browser-launch UX distinct from
  everything Portunus does today (closer to the `portunus ui open` shape from
  portunus-secure-entry than to anything auth-related that exists now).
- **Ongoing/non-engineering cost:** high, and **outside Portunus's control**. For any Google
  sensitive/restricted scope (Gmail chief among them): Testing-mode 7-day refresh-token expiry
  *or* full verification (2–6 week turnaround) + CASA Tier 2 security assessment for restricted
  scopes, plus periodic re-verification — a recurring administrative burden, not a one-time
  unlock. This cost is identical whether Portunus's engineering is excellent or mediocre; better
  code does not reduce it.
- **Risk:** medium. A bespoke consent-flow runner is new attack surface (a local HTTP listener
  during the flow, client-secret handling) that doesn't exist anywhere else in this codebase
  today.
- **Benefit:** highest ceiling — genuinely one-command OAuth login for any provider, no external
  tool dependency. But the benefit is gated by the Google verification cost above for exactly
  the use case (Gmail) that motivated this ask in the first place.
- **Verdict: not recommended.** The cost is dominated by a fee Portunus's own engineering cannot
  pay down, for the specific provider/scope the user actually asked about.

## Option B — storage + minting only; user bootstraps via existing tools (design-discussion.md §1.B/§2)

- **Engineering cost: low.** Storage and boundary-safe retrieval already exist and are already
  production-tested (`store_session`/`load_session`, research-brief.md §3) — reusing them is
  wiring, not new construction. New work is genuinely small: one `mint()`-shaped class mirroring
  `GCPWorkloadIdentityAuth`'s existing pattern (~50-80 lines, same transport-injection testing
  approach already used), one thin `SecretBackend` adapter (`OAuthBackend`), one new CLI command
  (`portunus oauth store`, a near-clone of `session store`'s existing shape), and documentation
  pointing at the provider-appropriate bootstrap mechanism. Rough order of magnitude: comparable
  to one of the smaller stories already shipped this session (e.g. `portunus-vault-transfer`
  Story 03), not a multi-week epic.
- **Ongoing/non-engineering cost: low for what it actually covers.** GCP-resource-scoped access
  already works today through `gcloud`'s already-verified client (`portunus auth login`'s
  existing precedent) — Option B extends that same, already-paid-for trust to any scope
  `gcloud auth application-default login --scopes=...` can request *that isn't itself
  sensitive/restricted*. For Gmail/Workspace-restricted scopes specifically, the same Google
  verification cost from Option A still applies to the bootstrap step — Option B doesn't make
  that cost disappear, it just correctly places it outside Portunus's own engineering effort,
  where it already, unavoidably lives.
- **Risk: low.** No new consent-flow surface; the new code is a `.access()` implementation
  following an already-established pattern, and injection reuses 100% of the existing
  boundary-safety machinery (§2 of design-discussion.md) with zero new sink types.
- **Benefit:** a real, shared, audited home for OAuth-backed credentials — one place to see
  what's stored, one boundary-safety discipline instead of per-project hand-rolled refresh
  scripts, and immediate value for any already-lightly-verified provider/scope (most non-Google
  APIs, and GCP-resource scopes today). For Gmail specifically: removes the *storage/refresh*
  friction, but the user still re-bootstraps every 7 days unless/until they personally complete
  Google's verification — a real, bounded improvement, not the full "log in once, forget it"
  outcome the original ask envisioned for Gmail specifically.
- **Verdict: recommended**, scoped honestly: build it as a generic OAuth broker; document the
  Gmail-specific 7-day-Testing-mode reality rather than promising more than Google's own policy
  allows.

## Option C — no shared mechanism, per-provider hand-rolled scripts

- **Engineering cost:** zero to Portunus; ongoing hand-rolled cost is spread across every project
  that needs this instead.
- **Risk:** highest of the three real options — no shared audit trail, no consistent
  encryption-at-rest guarantee, no boundary-safety discipline unless each script reinvents it.
- **Benefit:** whatever each ad-hoc script delivers, with none of the consolidation Portunus
  exists to provide.
- **Verdict: not recommended** — this is the status quo Portunus's own north star (a standalone,
  metadata-indexed secret manager consolidating exactly this kind of fragmentation) argues
  against by design.

## Recommendation

**Build Option B, scoped as design-discussion.md §2 describes, if and when OAuth-backed access
becomes a real recurring need** — it's cheap, low-risk, reuses already-proven primitives, and
delivers real value for GCP-adjacent and most non-Google-restricted-scope providers immediately.

**Set expectations correctly for the Gmail case that prompted this ask**: even with Option B
fully built, personal Gmail/Workspace-restricted-scope access still requires either re-bootstrapping
every 7 days (Testing mode) or the user personally completing Google's verification + CASA
process (weeks, non-engineering) to get an indefinite-lifetime token — that reality doesn't
change based on what Portunus builds. If the 7-day cadence is acceptable, Option B still removes
essentially all of the *ongoing* friction (auto-refresh between bootstraps, one shared place to
re-bootstrap from, audited access) even though the human still has to re-run the bootstrap
command weekly.

**Suggested next step, if this gets a go-ahead**: a standard `/plan` pass with real TDD stories
(mirroring `portunus-vault-transfer`'s 4-story shape: mint-class + tests, `OAuthBackend` +
router wiring, `portunus oauth store` CLI + docs, closeout/live-proof against a real GCP-scoped
token via `gcloud auth application-default login`) — deliberately proven first against a
scope/provider that has no Google-verification friction, before ever touching Gmail-specific
scopes, so the mechanism is validated independently of the one part of this that Portunus's own
engineering can't fix.
