# Structured Outline: portunus-standalone-core

## Part 1: Executive Summary

We're extending Portunus from a name-only secret registry into the standalone secret manager
the north star describes: structured metadata tags with fail-closed resolution, boundary-only
injection adapters for four target kinds (env var, file, HTTP header, HTTP body), a semantic
agent-facing front door, and a localhost UI — all without ever letting a plaintext value
transit an LLM/agent turn or a network hop. User sign-off on the design discussion confirmed
the 4-slice shape and locked six default calls: Next.js/React UI, `provider/project/env` +
open `tags{}` schema, folding `portunus-session-vault` stories 01/02 into Slice 1, a
CLI-first agent surface with a thin Claude skill wrapper, env+file adapters before
HTTP-header/body, and a coarse registry write lock added now rather than deferred.

Implementation strategy: build strictly bottom-up through the dependency chain identified in
the horizontal plan — registry schema and lock first (nothing else can be built or tested
against a moving target), then one consumer at a time (CLI query → adapters → semantic front
door → HTTP adapters → UI), closing with a glossary/verification/reconciliation pass. Every
phase below maps 1:1 to a vertical-plan.md step and produces a working, demoable state.

```
PRODUCT GOALS (optional):
  Success metrics: fail-closed resolution never returns >1 match silently; portunus verify
    passes against a chain containing every new entry type introduced by this epic; UI add
    flow never logs/caches a plaintext value
  Non-goals: L2 Pantheon plugin lifecycle wiring, remote/non-localhost UI deployment, GCP SM
    labels as tag source, MCP server for the agent surface (all explicitly deferred, see
    vertical-plan.md §4)
  Stakeholders: single-developer project (per north_star) — no cross-team sign-off needed
```

## Part 2: Detailed Approach

### Phase 1: Tag schema, migration, write lock

**Goal:** Registry supports structured tags with fail-closed resolution; zero behavior change
for existing callers.
**Depends on:** nothing (foundation phase)

**Changes**

1. `src/portunus/registry.py`
   - `Reference` dataclass gains `provider: str = ""`, `project: str = ""`, `env: str = ""`,
     `tags: dict = field(default_factory=dict)`. `scope`/`kind` stay, unchanged meaning.
   - New `resolve_by_tags(self, **partial_tags) -> Reference` on `Registry`: filters all
     references by the given key/value pairs (matching against provider/project/env/tags),
     raises `NoMatch` on zero results, `AmbiguousMatch` (lists candidate names, never guesses)
     on >1, returns the single `Reference` on exactly one.
   - New `migrate_legacy_tags(self) -> int` (or equivalent): one-time, additive pass that
     copies existing `scope`/`kind` values into `tags{}` (e.g. `{"scope": ref.scope, "kind":
     ref.kind}`) for every reference that has no tags yet. Returns count migrated. Idempotent
     — re-running is a no-op for already-migrated references.
   - Write lock: wrap registry-mutating methods (`add`, `set_state`, future tag-setters) in a
     file lock scoped to the registry file (e.g. `fcntl.flock` on a sidecar lock file under
     `PORTUNUS_HOME`). Lock acquisition failure raises a clear `RegistryLocked` error rather
     than hanging indefinitely (bounded timeout).

**Interfaces**

```python
# registry.py
class NoMatch(KeyError): ...
class AmbiguousMatch(KeyError):
    def __init__(self, candidates: list[str]): ...

def resolve_by_tags(self, **partial_tags: str) -> Reference: ...
def migrate_legacy_tags(self) -> int: ...
```

**Validation**
- Unit tests: 0/1/>1 match cases for `resolve_by_tags`; migration correctness against fixture
  registries with only scope/kind set; concurrent-writer test asserting the lock serializes
  two simultaneous `add()` calls without corrupting the registry file.
- What could silently break: a partial-tag query that's *supposed* to be ambiguous but isn't
  (matcher too loose) — cover this explicitly with a fixture that has two near-identical
  references differing only in `env`.

---

### Phase 2: CLI tag-query consumer

**Goal:** First real, working consumer of `resolve_by_tags()`.
**Depends on:** Phase 1

**Changes**

1. `src/portunus/cli.py`
   - New `find` subcommand: `portunus find --tags provider=vercel,project=mdostal.com` parses
     `--tags` into kwargs, calls `registry.resolve_by_tags(**kwargs)`, prints reference
     metadata (name, tags, state — never a value) or a clear error for `NoMatch`/`AmbiguousMatch`.

**Interfaces**
- CLI exit codes: 0 on match, non-zero (distinct codes for `NoMatch` vs `AmbiguousMatch`) so
  scripts can branch on the failure mode.

**Validation**
- CLI integration test covering all three outcomes (match / no-match / ambiguous).
- Manual: run against a local vault with 2+ tagged references from Phase 1's migration.

---

### Phase 3: Env + File injection adapters

**Goal:** Boundary-only injection into a process env var or a templated file.
**Depends on:** Phase 1, Phase 2

**Changes**

1. `src/portunus/resolver.py` (or a new `src/portunus/adapters.py` — file manifest below
   assumes the latter, keeping `resolver.py` focused on placeholder substitution)
   - `EnvVarAdapter.inject(value: str, var_name: str) -> None` — sets `os.environ[var_name]`
     in the *calling process* (distinct from today's subprocess-argv sink). Never returns the
     value; caller only gets a success/failure signal.
   - `FileAdapter.inject(value: str, path: str, fmt: Literal["env","json","yaml"], key: str)
     -> None` — writes a `0600` file at `path` in the requested format, templating `value`
     under `key`. Reuses the existing 0600-temp-file discipline from `resolver.py`, generalized
     to a caller-specified path/format instead of only a temp file.
2. `src/portunus/cli.py`
   - New `inject` subcommand: `portunus inject --tags ... --target env --var NAME` or
     `--target file --path ... --format env|json|yaml --key ...`.
3. `src/portunus/audit.py`
   - New entry type `adapter_resolution` (ref name, adapter kind, target descriptor — e.g.
     var name or file path, never the value — timestamp).

**Interfaces**

```python
class SecretAdapter(Protocol):
    def inject(self, value: str, **target_params: str) -> None: ...
```

**Validation**
- Boundary-invariant tests (the load-bearing verification for this phase): for each adapter,
  assert the function's return value, any exception message, and any log output never contain
  the injected value — use a sentinel value and grep all captured output/exceptions for it.
- `portunus verify` test: chain containing `adapter_resolution` entries still verifies clean.
- Manual: inject into a real env var (`echo $VAR` in the same shell context) and a real file,
  confirm value present only at the destination.

---

### Phase 4: Semantic front door

**Goal:** Natural-language request → concrete tag set → Phase 3 adapter dispatch, fail-closed
at the parsing step too.
**Depends on:** Phase 1, Phase 2, Phase 3

**Changes**

1. `src/portunus/resolver.py` (or `adapters.py`/new `intent.py`)
   - `parse_intent(text: str) -> dict[str, str] | AmbiguousIntent` — maps free text to a
     partial tag dict using deterministic keyword/pattern matching against known
     provider/project/env vocabulary already present in the registry (not a general NLP
     model — scope this to "recognize known tag values mentioned in the text," which is
     enough for "the vercel secret for mdostal.com" and fails closed on anything it can't
     confidently map).
2. `src/portunus/cli.py`
   - New `ask` subcommand: `portunus ask "<request>"` → `parse_intent` → `resolve_by_tags` →
     dispatches to the adapter implied by context (or a required `--target` flag if not
     inferable) → same audit trail as Phase 3.
3. Agent surface (new, outside `src/portunus/`, e.g. a Claude skill definition)
   - Thin skill wrapping `portunus ask` so an agent invokes a tool call rather than
     constructing a raw shell command.
4. `src/portunus/audit.py`
   - New entry type `semantic_op` (raw request text is NOT stored — only the resolved tag set
     and outcome, to avoid accidentally logging something sensitive typed into the request).

**Interfaces**

```python
class AmbiguousIntent(Exception):
    def __init__(self, clarifying_question: str, candidates: list[str] | None = None): ...
```

**Validation**
- Unit tests: known-vocabulary requests resolve correctly; requests naming two plausible
  provider/project combos raise `AmbiguousIntent` with a clarifying question, never a guess.
- `portunus verify` against a chain with `semantic_op` entries.
- Manual: 2+ deliberately ambiguous requests confirmed to prompt for clarification.

---

### Phase 5: HTTP header + body adapters

**Goal:** Extend the adapter set to outbound HTTP requests.
**Depends on:** Phase 3

**Changes**

1. `src/portunus/adapters.py`
   - `HttpHeaderAdapter.inject(value, request, header_name)` — sets a header on a
     caller-provided request object (library-agnostic: accepts a dict-like headers mapping,
     not a specific HTTP client, to avoid a new hard dependency).
   - `HttpBodyAdapter.inject(value, body: dict, json_path: str)` — sets a nested JSON field by
     path on a caller-provided dict.
2. `src/portunus/cli.py`
   - `inject --target http-header|http-body` extended options.

**Validation**
- Same boundary-invariant + audit-entry pattern as Phase 3, applied to both new adapters.
- Manual: inject into a request against a local test HTTP server, confirm receipt server-side
  and absence from any client-side log.

---

### Phase 6: UI v1

**Goal:** Localhost-only Next.js UI for view/add/rotate, routed through the existing gate.
**Depends on:** Phase 1, Phase 2, Phase 3, Phase 4

**Changes**

1. New `ui/` directory (Next.js/React app; exact structure finalized by the `/design`
   delegation triggered by story UI-keyword detection — see Part 8 decision 4)
   - Reference list screen (tags + state, never values)
   - Reference detail screen (audit trail for that reference)
   - Add-secret form → submits to a local API route that shells out to (or directly calls)
     the same harness-side-only `drop` path `cli.py` already uses — never a new privileged
     write path
   - Rotate action → triggers a server-side rotation flow where possible; falls back to the
     add-secret form's human-entry path when the backend can't generate a new value itself
2. `src/portunus/audit.py`
   - New entry type `ui_action` (ref, action kind, timestamp, actor="ui")
3. Backend-side test coverage confirming the UI's local API route calls `Broker.check_injectable`
   — i.e., a Python-side integration test against whatever local server the UI's backend uses,
   not just UI component tests.

**Validation**
- Manual: full add/view/rotate flow exercised locally end-to-end.
- Backend test: UI write path is provably gated (cannot bypass `Broker`).
- `portunus verify` against a chain with `ui_action` entries.

---

### Phase 7: Glossary, verification closeout, session-vault reconciliation

**Goal:** Epic closeout — nothing new shipped, everything already shipped gets reconciled.
**Depends on:** all prior phases

**Changes**

1. `.pHive/CONTEXT.md` — add Terminology entries for tag schema, adapter, `resolve_by_tags`,
   `parse_intent` (per Grill C1).
2. `.pHive/epics/portunus-session-vault/stories/01-arca-storage-model.yaml`,
   `02-session-api.yaml` — updated to reference the new tag schema (session credentials become
   a `kind`/`tags` combination) rather than re-implementing storage from scratch.
3. `.pHive/epics/portunus-session-vault/stories/03-ostiarius-gate.yaml` — re-evaluated: likely
   closed as superseded by the existing `Broker.check_injectable` gate, unless session-specific
   gating logic is found to be genuinely distinct.
4. `.pHive/epics/portunus-session-vault/stories/04-cli-broker-seam.yaml`,
   `05-playwright-integration-tests.yaml` — re-scoped against what actually shipped in Phases
   1-6, or explicitly closed if superseded.

**Validation**
- `portunus verify` run once against a chain spanning every entry type from Phases 1-6
  together (not just per-phase in isolation).
- Manual CONTEXT.md read-through against shipped code.

## Part 3: Verification Plan

```
Phase 1 verification:
  Automated: resolve_by_tags 0/1/>1-match unit tests; migration correctness tests;
    concurrent-write lock test
  Manual: none required — fully covered by automated tests
  Tools: pytest
  Platforms: n/a (library-level)

Phase 2 verification:
  Automated: CLI integration test (match/no-match/ambiguous)
  Manual: run portunus find against a local vault
  Tools: pytest

Phase 3 verification:
  Automated: boundary-invariant tests (sentinel-value grep across output/exceptions/logs),
    portunus verify against adapter_resolution entries
  Manual: real env var / real file injection, confirm value only at destination
  Tools: pytest

Phase 4 verification:
  Automated: parse_intent ambiguity-handling unit tests, portunus verify against semantic_op
    entries
  Manual: 2+ deliberately ambiguous requests
  Tools: pytest

Phase 5 verification:
  Automated: boundary-invariant tests for HTTP adapters, portunus verify against their entries
  Manual: injection against a local test HTTP server
  Tools: pytest

Phase 6 verification:
  Automated: backend test confirming UI write path is gated through Broker
  Manual: full add/view/rotate flow exercised locally
  Tools: pytest (backend), manual (UI — no UI test framework chosen yet, see Part 6 blocking
    question)

Phase 7 verification:
  Automated: full-chain portunus verify across all new entry types together
  Manual: CONTEXT.md read-through
```

```
| Acceptance Criterion | Test Type | Tool | Phase |
|---|---|---|---|
| resolve_by_tags fails closed on ambiguity | Unit | pytest | 1 |
| Legacy references migrate additively | Unit | pytest | 1 |
| Concurrent writers don't corrupt registry | Unit | pytest | 1 |
| portunus find works end-to-end | Integration | pytest | 2 |
| Adapters never leak the value | Boundary-invariant | pytest | 3, 5 |
| portunus verify passes on new entry types | Integration | pytest | 3, 4, 5, 6, 7 |
| parse_intent fails closed on ambiguity | Unit | pytest | 4 |
| UI write path is gated (no bypass) | Integration | pytest | 6 |
| Full add/view/rotate UI flow works | Manual | manual | 6 |
```

**What's NOT being verified and why:** UI automated component/E2E testing — no UI test tooling
exists yet in this repo (it's a net-new Next.js app); Phase 6 relies on manual verification for
v1, with automated UI tests flagged as a fast-follow once the UI's shape stabilizes (this is a
question for the `/design` delegation and the UI designer, not decided here).

## Part 3b: Cross-Cutting Concerns

- **Error handling strategy:** every new failure mode (`NoMatch`, `AmbiguousMatch`,
  `AmbiguousIntent`, `RegistryLocked`, adapter injection failure) is a distinct, catchable
  exception type — never a bare string match on an error message. CLI subcommands map each to
  a distinct exit code.
- **Migration plan:** Phase 1's `migrate_legacy_tags()` is additive-only (never deletes
  `scope`/`kind`), idempotent, and run automatically on first access to a pre-existing
  registry rather than requiring a manual step — so no existing user/repo needs an explicit
  migration command.
- **Rollback plan:** every phase is additive to the existing CLI/API surface (no existing
  subcommand's behavior changes). Rolling back any single phase means reverting that phase's
  commit(s); no other phase depends on a phase being *rolled back cleanly*, only on it having
  shipped.
- **Performance implications:** the registry write lock (Phase 1) adds contention only on
  writes, not reads — reads (including `resolve_by_tags`) stay lock-free. Single-developer
  scale means lock contention is not expected to be observable in practice.
- **Documentation impact:** README.md's "Usage" section should gain `find`/`inject`/`ask`
  examples once Phases 2-4 ship (flagged for the `documentation` cross-cutting concern at the
  per-story level, not written here).
- **Security considerations:** every new adapter and the semantic front door are new attack
  surface for accidental value exposure — this is why boundary-invariant tests are mandatory
  (not optional) for Phases 3-5, and why Phase 4's audit entry deliberately excludes the raw
  request text.

## Part 4: File Change Manifest

```
FILES:

CREATE:
  - src/portunus/adapters.py — EnvVarAdapter, FileAdapter, HttpHeaderAdapter, HttpBodyAdapter
  - tests/test_adapters.py — boundary-invariant + functional tests for all four adapters
  - src/portunus/intent.py — parse_intent(), AmbiguousIntent
  - tests/test_intent.py — parse_intent tests
  - tests/test_registry_tags.py — resolve_by_tags, migration, concurrency tests
  - tests/test_cli_find.py, tests/test_cli_inject.py, tests/test_cli_ask.py — CLI integration tests
  - ui/ (new Next.js app — exact file tree owned by the /design delegation + implementation)
  - ui/tests/... (backend-route gating tests live in tests/test_ui_gate.py under the existing
    pytest suite; frontend test tooling TBD per Part 3's open item)
  - .claude/skills/portunus-ask/ (or equivalent) — thin Claude skill wrapping `portunus ask`

MODIFY:
  - src/portunus/registry.py — Reference schema, resolve_by_tags, migrate_legacy_tags, write lock
  - src/portunus/cli.py — find, inject, ask subcommands
  - src/portunus/audit.py — adapter_resolution, semantic_op, ui_action entry types
  - src/portunus/resolver.py — wire adapters in as new sinks alongside existing three
  - .pHive/CONTEXT.md — Terminology additions (Phase 7)
  - README.md — usage examples for find/inject/ask (flagged, not detailed here)
  - .pHive/epics/portunus-session-vault/stories/*.yaml — reconciled per Phase 7
  - pyproject.toml — no new Python deps expected for Phases 1-5; revisit if Phase 6's backend
    route needs a lightweight web framework beyond argparse/stdlib

UNCHANGED (but affected):
  - src/portunus/broker.py, backend.py, localvault.py — consumed as-is by every new path;
    verify no new caller bypasses Broker.check_injectable
```

## Part 5: Risk Registry

| # | Risk | Severity | Likelihood | Mitigation | Owner |
|---|------|----------|------------|------------|-------|
| 1 | `resolve_by_tags` matcher too loose, ambiguity slips through | high | low | Fixture tests with deliberately near-identical references; matcher logic reviewed explicitly for this case (Phase 1) | Phase 1 developer + reviewer |
| 2 | An adapter leaks a value via an exception message or log line | high | medium | Sentinel-value boundary-invariant tests mandatory, not optional, for every adapter (Phase 3, 5) | Phase 3/5 developer + reviewer |
| 3 | UI's local API route bypasses `Broker.check_injectable` | high | medium | Backend-side gating test (Phase 6) is a merge-blocking check, not a nice-to-have | Phase 6 developer + reviewer |
| 4 | Registry write lock deadlocks or hangs indefinitely under a bug | medium | low | Bounded lock-acquisition timeout with a clear `RegistryLocked` error, never an infinite wait (Phase 1) | Phase 1 developer |
| 5 | `parse_intent`'s deterministic matching is too rigid to be useful (over-corrects for H2/U2 by failing closed on everything) | medium | medium | Build the known-vocabulary list from actual registry tag values so it grows with real usage; treat "too strict" as an acceptable v1 trade-off vs. "too loose" | Phase 4 developer |
| 6 | Session-vault reconciliation (Phase 7) surfaces more rework than a simple fold-in | low | medium | Scoped as its own phase at the end, not blocking Phases 1-6; can spin out as a follow-up epic if needed (see vertical-plan.md moldability notes) | Phase 7 developer |

## Part 6: Dependency Map

```
INTERNAL DEPENDENCIES:
  Phase 2 depends on Phase 1 (resolve_by_tags)
  Phase 3 depends on Phase 1, Phase 2 (adapters consume resolved References)
  Phase 4 depends on Phase 1, Phase 2, Phase 3 (semantic front door dispatches to adapters)
  Phase 5 depends on Phase 3 (adapter abstraction)
  Phase 6 depends on Phase 1, Phase 2, Phase 3, Phase 4 (UI calls the stable API surface)
  Phase 7 depends on all prior phases (closeout)

EXTERNAL DEPENDENCIES:
  None new for Phases 1-5 (stdlib + existing `cryptography` dependency suffice)
  Phase 6: Next.js/React (new Node toolchain, isolated to ui/) — exact versions TBD in
    /design delegation

BLOCKING QUESTIONS:
  - CI coverage for the ui/ Node toolchain — same ci.yml gate or a separate workflow?
    (flagged in horizontal-plan.md, not yet decided — does not block Phases 1-5)
  - UI automated test tooling choice — deferred to the /design delegation for Phase 6
```

## Part 7: Elicitation — Stress-Testing This Plan

#### Why Won't This Work?

1. **Failure:** `resolve_by_tags`'s matcher is subtly too permissive (e.g., substring match
   instead of exact-value match), so a query intended to be ambiguous returns exactly one
   "close enough" result.
   - **Trigger:** careless implementation of the tag-matching logic in Phase 1.
   - **Impact:** the single most dangerous failure mode in this entire epic — silently
     injects the wrong secret.
   - **Signal:** Phase 1's fixture tests with near-identical references (Risk #1) catch this
     before any downstream phase builds on it, *if* the fixtures are adversarial enough.
   - **Our answer:** we believe this is caught early because Phase 1 is isolated and
     exhaustively unit-tested before any consumer exists — but this is the one place where
     "our answer" should not be trusted blindly; recommend an explicit adversarial-fixture
     review during Phase 1's code review, not just green tests.

2. **Failure:** An adapter's error path (not the happy path) leaks the value — e.g., an
   exception's `str()` representation includes the value because of a careless f-string.
   - **Trigger:** a developer writing `raise AdapterError(f"failed to inject {value} into
     {target}")` instead of omitting the value from the message.
   - **Impact:** violates the core non-negotiable invariant; would be a severe regression.
   - **Signal:** boundary-invariant tests (Phase 3/5) that specifically exercise the *failure*
     path of each adapter (not just success), grepping exception output for the sentinel.
   - **Our answer:** Part 3b already flags this; Phase 3/5's test requirement explicitly
     includes failure-path coverage, not just happy-path.

3. **Failure:** The registry write lock causes a hang in an interactive CLI session (e.g., a
   human leaves a `drop` command paused at a prompt while holding the lock, then a UI request
   comes in and blocks indefinitely).
   - **Trigger:** any long-lived process holding the lock without a bounded wait on the
     other side.
   - **Impact:** UI/agent requests appear to hang with no explanation.
   - **Signal:** Risk #4's bounded-timeout requirement; if it fires in practice, the error
     message should be immediately actionable ("registry locked by another process, retry").
   - **Our answer:** bounded timeout + clear error is the mitigation; we're not implementing
     lock-holder identification (e.g., "locked by PID 1234") for v1 — acceptable simplicity
     trade-off for single-developer scale.

4. **Failure:** `parse_intent` is built against too small a vocabulary sample and fails closed
   on requests that feel like they *should* work, frustrating the "ask semantically" north-star
   goal into uselessness.
   - **Trigger:** vocabulary list built from a small number of test-fixture references rather
     than realistic tag diversity.
   - **Impact:** doesn't break anything dangerous, but undermines the point of Phase 4 — a
     usability failure, not a safety failure.
   - **Signal:** manual testing (Phase 4) against realistic-sounding requests, not just the
     unit-test fixture set.
   - **Our answer:** acceptable to ship stricter-than-ideal in v1 (Risk #5's stance) — a
     usability gap is fixable later; a safety gap is not.

5. **Failure:** The UI's add-secret form silently becomes a second privileged path if a future
   change (post-epic) adds a convenience shortcut that bypasses `Broker.check_injectable`.
   - **Trigger:** not this epic's Phase 6 code as planned — a *future* regression.
   - **Impact:** the exact "second front door with different rules" risk flagged in the
     design discussion's §4.
   - **Signal:** Phase 6's backend gating test (Risk #3) is the durable guardrail — it should
     stay in the test suite permanently, not be treated as a one-time check.
   - **Our answer:** this is why Risk #3's test is called out as merge-blocking rather than
     advisory — it needs to keep failing loudly if a future change removes the gate.

#### What Assumptions Are We Making?

- **VERIFIED** — GCP Secret Manager supports native resource labels (confirmed via general
  GCP SM knowledge; not independently re-verified against live docs since Slice 1-5 don't
  depend on this — it only informed the Grill H1 decision to reject SM labels as the tag
  source).
- **VERIFIED** — `Reference`'s current fields (`scope`, `kind`, `state`, `approval`,
  `sm_path`) per direct inspection of `registry.py`.
- **ASSUMED** — a coarse file lock (e.g. `fcntl.flock`) is sufficient for registry
  concurrency at single-developer scale; we're not building a proper transactional store.
  Comfortable proceeding because the north_star explicitly states single-developer/low-volume
  scale today.
- **ASSUMED** — `parse_intent`'s deterministic keyword-matching approach (not a general NLP
  model) is sufficient for v1's "semantic" bar. Comfortable proceeding because the fail-closed
  requirement means the cost of being wrong is a clarifying question, not a wrong injection.
- **RISKY** — Phase 6's UI stack (Next.js/React) is the user's own stated preference match to
  the existing Pantheon dashboard ecosystem, but this structured outline doesn't independently
  verify that ecosystem's exact stack/version. If wrong, Phase 6 changes stack but Phases 1-5
  are unaffected (UI is architecturally isolated behind a stable API surface).
- **RISKY** — the plan assumes `portunus-session-vault`'s stories 01/02 fold cleanly into the
  new tag schema. If the session-vault storage model has a structural requirement that doesn't
  map onto `provider/project/env/tags`, Phase 7 could expand into real rework rather than a
  quick fold-in (Risk #6 already flags this).

#### What's the Simplest Version?

- **Must have:** Phase 1 (tag schema + fail-closed resolution) and Phase 3 (env+file
  adapters) — these two alone deliver the core "find by metadata, inject at the boundary"
  promise, which is the north star's single most-repeated ask.
- **Should have:** Phase 4 (semantic front door) — meaningfully improves the "agent asks
  semantically" experience over raw `--tags` flags, but Phase 2's exact-tag CLI already
  delivers a working (if less ergonomic) version of the same capability.
- **Could cut (from this epic, not forever):** Phase 5 (HTTP adapters) and Phase 6 (UI) are
  the two most cuttable if scope needs to shrink — Phase 5 because env/file adapters already
  prove the adapter pattern, Phase 6 because it's the largest, riskiest, most isolable piece
  (see vertical-plan.md moldability notes, which already flag both as reorderable/droppable).

#### What Will We Wish We Had Thought Of?

- **Technical debt:** the `tags{}` open dict alongside structured `provider/project/env`
  fields is a deliberate hedge (Q2's default call) — we'll likely end up wishing we'd picked
  one shape earlier once real usage shows which fields matter. Trade-off accepted because
  guessing wrong on a rigid schema is more expensive to fix than a hedge.
- **Edge cases deferred:** secret *rotation* where the backend can't generate a new value
  server-side (Phase 6 falls back to human entry) is under-specified — safe to defer because
  it only affects the UI's rotate flow, not the core resolution/injection path.
- **Integration points not fully validated:** the UI's exact backend-route mechanism (local
  Next.js API route calling into Python, vs. a small local HTTP server, vs. shelling out to
  the CLI) isn't pinned down here — deliberately left to the `/design` delegation, which will
  have more context once the UI's concrete shape exists.
- **User workflows not considered:** multi-repo usage (the user's stated eventual goal —
  "across all repos") isn't addressed by this epic; every phase here assumes a single
  `PORTUNUS_HOME` vault. Cross-repo/cross-vault federation is explicitly out of scope and
  should be flagged as a likely next epic once this one ships.

#### Where Are We Over-Engineering?

- The `SecretAdapter` Protocol (Phase 3) has four consumers by Phase 5 (env/file/http-header/
  http-body) — not a speculative abstraction, justified by actual fan-out, keeping it.
- `parse_intent` deliberately avoids pulling in an NLP/ML dependency — this is the
  *opposite* of over-engineering; flagging it here as a conscious minimalism choice, not a gap.
- The write lock (Phase 1) is intentionally coarse (whole-registry, not per-reference) rather
  than building fine-grained row-level locking — right-sized for single-developer scale; would
  be over-engineering to build finer-grained locking without evidence of real contention.

## Part 8: Decision Points for Sign-Off

```
DECISIONS REQUIRING SIGN-OFF:

1. [APPROACH] Bottom-up phase ordering (registry -> CLI query -> adapters -> semantic ->
   HTTP -> UI -> closeout) vs. building UI earlier in parallel with backend phases.
   We chose bottom-up because Phase 6 (UI) depends on Phases 1-4's API surface being stable;
   building it earlier risks throwaway UI work against a moving target.
   → Affirm / Change direction

2. [SCOPE] Phase 5 (HTTP adapters) and Phase 6 (UI) are the two most cuttable if time is
   short (see Part 7 "Simplest Version"). Phases 1-4 alone deliver the core metadata-lookup +
   injection + semantic-ask north-star capabilities without a UI.
   → Affirm / Adjust scope

3. [RISK ACCEPTANCE] Accepting a coarse, whole-registry file lock (not fine-grained) and a
   deterministic (non-ML) `parse_intent` for v1, both justified by single-developer scale.
   → Accept / Require mitigation

4. [DEFERRED DECISION] Phase 6's exact UI backend-route mechanism and automated test tooling
   are left open for the `/design` delegation rather than pinned down here.
   → Affirm / Decide now instead
```
