# Design Discussion — portunus-metadata-crawl

## 1. `sm_name` is the strongest available signal, not `group`

Sampling the real vault turned up something research-brief.md's field-fill-rate table alone
didn't show: for GCP-discovered references, `sm_name` is literally the environment variable
name (`AUTH_SECRET`, `DEVTO_API_KEY`, `GOOGLE_CLIENT_SECRET`, `FLAYR_API_KEY`) — a highly
semantic, per-reference signal, stronger than `group` (which is app/service-level, not per-
secret). Both matter: `group` supplies project/app/env structure, `sm_name` supplies what the
individual secret actually is.

## 2. The crawl does NOT run an LLM inside Portunus — it bundles context for one

Turning `AUTH_SECRET` into "Authentication secret for X" is squarely an LLM's job, not a regex/
heuristic parser's — and Portunus already has the exact right interface for an LLM to act on
it: `portunus_suggest_metadata` (MCP tool, shipped). Building a second, bespoke naming-
convention parser inside Portunus would duplicate what an LLM already does better, and this
codebase has zero LLM-API-key infrastructure anywhere (deliberately — no API key management,
no LLM call sites exist today).

**Decision:** `portunus crawl` is a *discovery* tool, not a suggestion tool. It finds references
missing metadata and bundles everything ALREADY KNOWN about each — `sm_name`, `group`,
`project`, `org`, `repo`, `source_files`, the project's `VaultBinding` (backend/sync_mode/
account/wif_audience), and the reference's `RotationBinding` if its `provider` has one
(status/account) — into one JSON response. An LLM (Claude Code, another MCP-connected agent, or
a human reading the output) reads that bundle and calls the ALREADY-SHIPPED
`portunus_suggest_metadata` for each reference it has an opinion on. No new write path, no new
LLM-call infrastructure inside Portunus itself — `crawl` only ever reads and bundles metadata
that already exists, never a value, and never writes anything on its own.

This directly matches the user's own framing: *"portunus should be the top level config expert"*
(crawl assembles everything Portunus already knows into one place) *"and then let the human
verify"* (nothing is written until `metadata confirm`, same as today) — with the LLM as the
thing doing the "going back and forth," using the same MCP tool this epic doesn't need to touch.

## 3. "Other workers/consumers" — surfaced from what's already tracked, not a new store

The user's clarification named GitHub Actions, Vercel, WIF, "integrations" as things that
*consume* a secret. Two real, already-existing sources cover a meaningful slice of this without
inventing anything: `VaultBinding.wif_audience`/`.account` (which WIF pool/identity a project's
GCP access goes through) and `RotationBinding.provider`/`.account` (which external system a
reference's rotation would target, when configured — Vercel/GitHub/Stripe today). `crawl`
surfaces both in its bundle. Deeper consumer discovery (parsing a real `.github/workflows/*.yml`
or `vercel.json` for an env var reference) is real, wanted, explicitly out of v1 — gated on
`repo` having real fill-rate first (§3 below), which this epic's own crawl bundle can help
build toward (an LLM reading a `group` like `ffe-cicd/event-api/prod` can reasonably propose a
`repo` guess for a human to confirm, same suggest/confirm path as any other field).

## 4. The report is a renderer over whatever the vault already knows, not gated on the crawl

**Decision:** `portunus report [--org X] [--project X] [--out path]` renders current vault state
— org → project → env structure, description/purpose where present, provider/backend routing,
rotation status, and an explicit gap list (references still missing metadata) — as Markdown.
Useful today, with zero crawl-inferred data, as a real "deploy docs" starting point (the user's
own framing: *"give out a report for us to make deploy docs on a company that doesn't have
them"*); gets richer as `crawl`-sourced suggestions get confirmed over time. Read-only, no
mutation, matches `portunus tree`'s own metadata-only posture — never a value.

## 5. UI surface: expose the crawl bundle, don't fake automation the codebase can't yet do

**Decision:** a "Crawl" section (Settings page) lists reference names still missing metadata
(reusing Slice 2's completeness derivation, already computed client-side) with a button that
fetches `/api/crawl`'s bundle and lets a human copy/view it — framed honestly as "context for an
LLM session," not a magic auto-fill button, since there's no LLM call happening inside the UI
itself. A "Download report" button hits `/api/report`. Neither route touches a value; both are
thin shells over the same CLI/MCP primitives every other route already uses.

## 6. Self-grill

- *Does bundling `VaultBinding`'s `wif_audience`/`account` into the crawl output leak anything
  sensitive?* No — both are already returned in full by the existing `GET /api/bindings` (design-
  discussion.md of portunus-bindings-settings-ui already established both are non-credential
  identity/topology strings). Crawl's bundle carries the same trust level as data already
  exposed elsewhere, not a new exposure.
- *Should `crawl` accept a `--repo <path>` to actually scan a local checkout in v1?* Deliberately
  no — real fill-rate (1/393 references have `repo` set) means there's almost nothing to point a
  scanner at yet. Shipping a real repo-scanner against a feature with near-zero real data to
  exercise it would be exactly the kind of premature, unvalidated work this project's own
  precedent (portunus-provenance-graph's dry-run-first discipline, the swappable-trio stub-
  before-real posture) argues against. Once `repo` fill-rate rises (helped by this epic's own
  bundle informing LLM-proposed `repo` guesses), a real crawler is a well-motivated follow-up.
- *Is a "discovery bundle, not an automated writer" a weaker deliverable than what the user
  asked for?* A real tension, named directly. But it's the version that's actually buildable
  without inventing LLM-orchestration infrastructure this codebase doesn't have, and it composes
  correctly with everything already shipped (suggest/confirm, the MCP tool surface) rather than
  duplicating it. The "going back and forth with the LLM" the user described IS this shape —
  crawl bundles context, an LLM (like this very session, via the MCP tool) proposes, a human
  confirms — just without Portunus itself embedding a model call.

## 7. Scale assessment

**Medium.** Story 1 (crawl bundle) and story 2 (report) are both read-only, metadata-only,
additive CLI/MCP/API surfaces over data that already exists — no new store, no new lock, no new
write path. Story 3 (UI) is a thin, honest surface over both. `version_bump: minor`.
