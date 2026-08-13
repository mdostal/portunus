# Design Discussion: portunus-standalone-core

## 1. What Are We Doing?

We're turning Portunus from "a Dostal-harness plugin with a registry and a resolver" into
what the north star actually asks for: a standalone secret manager you can point at any repo,
that finds secrets by *what they're for* instead of exact names, injects them straight into
the real target (an HTTP call, an env var, a file) without a value ever passing through an
LLM turn, and gives a human a UI to see/add/move/roll them. The Dostal-harness "Vault" tab is
a secondary surface on top of this, not the reason it exists.

Concretely, "done" for this epic means: I can ask (as an agent, semantically) "get me the
vercel secret for mdostal.com" and get it injected into the right place with zero plaintext in
my context; a human can open a UI and see every reference, add one, roll one, without ever
being handed a raw value either (the UI reads references + audit trail, writes go through the
same gated broker); and the existing safety invariants (boundary-only, audit-chained,
fail-closed) hold for every new path we add.

## 2. What I Found

The core loop already exists and is solid: `Registry` (reference metadata, no values) →
`Resolver`/OSTIARIUS (`{{secret:NAME}}` substitution) → `Broker`/Petitio (gate) →
`SecretBackend`/ARCA (`MockBackend` | `LocalEncryptedBackend` | `GcloudBackend`) →
`AuditChain`. Every resolved value's only sinks today are a caller-supplied callable, a
`0600` temp file, or an exec'd subprocess's argv (`resolver.py` docstring, lines 11-16) — that
invariant is well-documented and I'm not touching it, only adding more sinks that obey it.

`Reference` (`registry.py`) already carries `scope` (freeform: "shared" or a client slug) and
`kind` (freeform: `gemini | anthropic | linear | slack | ...`). That's a real foundation for
tagging, but it's two loose strings, not a structured schema — there's no `provider`, no
`env`, no way to query "give me the one match for these three tags" and fail closed on more
than one hit. `GcloudBackend.access()` (`backend.py:44-69`) only fetches by exact `sm_name` —
it doesn't read GCP Secret Manager's own resource labels, so metadata lookup has to live in
Portunus's own registry, not be delegated to SM.

There is genuinely no UI in this repo. `manifest.json`'s `ui.mount: "link"` points *out*
somewhere — this repo has never built the UI it advertises.

*(Grill H1)* GCP Secret Manager does support native resource labels, and I considered using
those as the tag source instead of extending Portunus's own registry. I'm rejecting that:
`LocalEncryptedBackend` (the default, Stage 1 tier) has no GCP-side resource to label at all,
so SM labels can't be the tag source without making tag lookup GCP-only — which breaks the
default backend. Tags live in Portunus's own registry, backend-agnostic, full stop.

There's also no semantic write path for agents. `drop` (cli.py) is explicitly
harness-side-only — stdin/file, never a value through an LLM turn — and that's correct and
should stay true for adding/rotating too. What's missing is the *shape* of that operation as
something an agent can invoke by intent ("add a secret for X") rather than a human running a
CLI command by hand.

`.pHive/epics/portunus-session-vault/` (5 TDD stories: Arca storage model, session API,
role-scoped gate, CLI/broker seam, Playwright E2E) pre-dates this reframe. It's not wrong —
session credentials are a secret *kind* like any other — it was just planned before we knew
secrets needed structured tags and injection adapters. Stories 01/02 (storage model, session
API) look like they'd become a specialization of whatever generalized schema this epic lands
on; story 03 (role-scoped gate) overlaps with the broker's existing approval gate rather than
needing its own mechanism.

## 3. My Proposed Approach

I'd split this into four vertical slices, each landing in a working state:

**Slice 1 — Structured metadata + tag resolution.** Extend `Reference` with a proper tag
schema (`provider`, `project`, `env`, plus the existing `scope`/`kind` kept for back-compat)
and add a `resolve_by_tags()` path in `registry.py` that takes a partial tag set and returns
exactly one match or raises on zero/ambiguous matches (fail-closed, matching the existing
`UnknownReference` pattern in `resolver.py`). This is the foundation everything else builds on
— get it right first, in isolation, fully unit-tested, no new sinks yet.

**Slice 2 — Injection adapters.** Add an adapter abstraction next to the existing three sinks:
HTTP header, HTTP body/JSON field, process env var (distinct from the current subprocess-argv
sink), and a templated file sink (`.env`, JSON, YAML). Each adapter is a small, single-purpose
class the resolver hands a value to at the boundary — never returns it. Every adapter gets the
same audit-chain entry (ref, target *kind*, timestamp — never the value) that resolution
already produces.

**Slice 3 — Agent-facing semantic operations.** A thin layer (probably a CLI subcommand plus
whatever skill/tool surface the harness needs) that lets an agent ask by intent — "inject the
vercel secret for mdostal.com into this env" or "I need a new secret for X, here's where it
goes" — without ever putting a value in the request/response. Adding/rotating still goes
through the same gated, harness-side-only path as `drop` today; this slice is about giving
that path a semantic front door instead of requiring a human to run the raw CLI.

*(Grill U2)* A natural-language request like "the vercel secret for mdostal.com" has to be
parsed into a concrete tag set before it ever reaches `resolve_by_tags()`. That parsing step
must itself fail closed: if it can't confidently map the request to a single tag combination,
it returns a clarifying question (to the agent or human), never a best guess. "No fuzzy
fallback, ever" (Slice 1) is an end-to-end property, not just a property of the matching
function — the parser doesn't get an exemption.

**Slice 4 — UI.** A small standalone app (tech choice is an open question, see §6) that reads
the registry + audit chain (read-only for viewing) and can perform add/move/roll operations.

*(Grill U1)* Resolving the "UI never handles values" tension: a human has to originate a
plaintext value from *somewhere* to add or rotate a secret, and I'm deciding that the UI's
"add" form is that entry point — the browser is the boundary-adjacent human surface, exactly
analogous to a human running `portunus drop --stdin` today. That means: the add/rotate flow
submits directly to the same harness-side-only local endpoint `drop` already uses (never
through an LLM/agent), the UI must be localhost-only (never a value transiting a remote
network hop), and the value must not be logged, cached, or sent to any analytics/telemetry
inside the UI app itself. Every other UI operation (view references, browse audit trail,
trigger a roll where the *new* value is generated server-side rather than human-entered)
stays strictly read-only-for-values. This needs to be an explicit constraint in Slice 4's
stories, not an assumption.

The L2 Pantheon plugin lifecycle (`manifest.json` wiring) is explicitly **not** in this epic —
north star says it's secondary, and building it before the standalone core exists would be
building the plugin surface for a product that doesn't work standalone yet.

## 4. What Could Go Wrong

- **[high] Ambiguous tag resolution silently picks the wrong secret.** This is the whole ballgame
  — "inject-the-wrong-secret is worse than no broker" (the user's own framing). Slice 1's
  `resolve_by_tags()` must hard-fail on any ambiguity, no fuzzy best-guess fallback, ever.
- **[high] A new adapter becomes a new leak path.** Every adapter in Slice 2 has to be
  reviewed against the same boundary invariant as the existing three sinks — HTTP body/JSON
  templating in particular is exactly the kind of code that "helpfully" logs a request body
  somewhere. Needs explicit tests asserting no leak, not just happy-path tests.
- **[medium] Backward compatibility of `scope`/`kind`.** Existing references (and the
  session-vault epic's assumptions) use the old two-string shape. Slice 1 needs a migration
  story, not a silent schema break.
- **[medium] UI becomes a second privileged path.** If the UI ever bypasses the broker gate
  "for convenience," we've built a second front door with different rules. Slice 4 must call
  the same Broker/Resolver path as everything else.
- **[low] GCP Secret Manager label drift.** If we ever want SM's own labels to be a source of
  truth instead of Portunus's local registry, that's a bigger redesign — out of scope here,
  flagged for later (see §2's Grill H1 resolution for why local-registry-owned tags won this
  round).
- **[medium] Concurrent writers to the local registry.** *(Grill H2)* Today only the CLI
  writes to the file-backed registry/vault. This epic adds a UI and an agent-facing surface as
  additional writers. No locking/serialization exists today. Slice 1 needs to either add a
  write lock (even a coarse file lock is enough for a single-developer-scale project) or the
  team explicitly accepts single-writer-at-a-time as a v1 constraint and documents it — silent
  races are not acceptable given what's at stake.

## 5. Dependencies and Constraints

- Slice 1 blocks everything else — tag resolution has to exist before adapters or UI can use it.
- Slice 4 (UI) depends on Slices 1-3 having a stable API surface to call into; building UI
  against a moving target wastes work.
- No new third-party library is implicated for Slices 1-3 (pure extension of existing Python
  modules). Slice 4's stack is an open question (§6) and may pull in a new dependency.
- `cross-cutting-concerns.yaml`'s `secret-boundary-invariant` and `audit-chain-integrity`
  apply to every story in Slices 2-4.
- *(Grill C1)* This epic introduces real new domain vocabulary (tag schema, injection
  adapter, `resolve_by_tags`). Per `.pHive/CONTEXT.md`'s own update triggers, a story late in
  Slice 1 (once the schema stabilizes) should update the Terminology section — not deferred
  indefinitely.
- Decision needed on `.pHive/epics/portunus-session-vault` before story decomposition: fold
  its stories into this epic's Slice 1 (recommended — session credentials become a `kind`
  under the new tag schema) or keep it as a separate, later epic that consumes Slice 1's API.

## 6. Open Questions

1. **UI stack** — standalone web app (Next.js? matches the existing Pantheon dashboard stack
   the user mentioned), a local-only desktop-ish tool, or a lightweight server the existing
   `manifest.json` `ui.mount: link` points at? This materially changes Slice 4's scope.
2. **Tag schema exact shape** — is `provider/project/env` sufficient, or do we need an open
   `tags: {}` dict for forward-compat with integrations we haven't thought of yet?
3. **`portunus-session-vault` disposition** — fold into this epic's Slice 1, or keep separate
   and re-point it at the new API once Slice 1 ships? (My read: fold in — see §5.)
4. **Agent-facing surface for Slice 3** — a Claude skill, an MCP server, both? The north star
   mentions "LLM capable skills and tools" but doesn't pin down which.
5. **How aggressively to scope Slice 2's adapters for v1** — all four (header/body/env/file)
   in one slice, or ship env+file first (lowest risk, matches today's sinks most closely) and
   add HTTP header/body as a follow-up slice?
6. *(Grill H2)* **File-lock vs. accepted single-writer constraint** — does Slice 1 add real
   write-serialization for the registry/vault now that UI + agent surface + CLI can all write,
   or do we explicitly scope v1 to "one writer at a time, user's responsibility," and revisit
   if it bites us?

## 7. Verification Strategy

```
VERIFICATION PLAN:
  Tools: pytest (existing), new adapter-specific leak-assertion tests
  Platforms: n/a (CLI/library + eventual UI, no mobile/browser matrix yet)
  Automated: tag-resolution ambiguity handling (Slice 1), every adapter's
    boundary-invariant (Slice 2: assert no return/log/print of value),
    audit-chain entries for new paths (Slice 2/3), backward-compat migration
    of existing references (Slice 1), `portunus verify` passing against a
    chain that includes adapter and semantic-op entry types (Grill C2 — per
    cross-cutting-concerns.yaml's audit-chain-integrity checklist)
  Manual: UI add/move/roll flows (Slice 4) until UI test tooling exists
  Not verifying: GCP Secret Manager label-as-source-of-truth (out of scope),
    L2 plugin lifecycle (out of scope this epic)
```

## 8. Scale Assessment

```
SCALE ASSESSMENT:
  Files affected: ~15-25 (registry/resolver/broker/backend extensions,
    new adapter module(s), new UI app, new agent-facing surface, tests for all)
  Subsystems: registry/metadata, resolver/injection, new UI, new agent-facing
    interface, audit chain (touched by all)
  Migration required: yes — existing references need scope/kind -> tag schema migration
  Cross-team coordination: no (single-developer project per north_star), but
    Slice 4 UI stack choice has ripple effects on Slice 3's interface shape
  Unknowns: 5 (see Open Questions) — UI stack and adapter v1 scope are the two
    that most affect sizing

  RECOMMENDATION: Needs structured outline
  RATIONALE: Multi-system (new UI + new adapter framework + new agent-facing
    surface), a real migration, and enough open questions (UI stack especially)
    that a full H/V slice plan plus structured outline will surface risks and
    sequencing that a design discussion alone won't catch. This is Large scope.
```
