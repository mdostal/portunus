# Design Discussion: portunus-vault-metadata

## 1. What Are We Doing?

Completing the piece of the north star every prior epic has deferred: ARCA stops being "one
GCP tier plus a local-encrypted tier" and becomes a real multi-backend vault registry with rich
metadata, and OSTIARIUS grows a metadata-only query surface an LLM can actually use — "what
secrets exist for project X, what are they for, where do they go" — without ever touching a
value. Concretely: (1) `Reference` gains description/purpose/injection-target metadata, (2) a
real keyless GCP Secret Manager backend replaces the bare single-project `gcloud` shell-out,
(3) a read-only discovery command surfaces what already exists in a live GCP project so it can
be registered instead of re-created blind, (4) a metadata-only "list keys for project" query
an agent can call, (5) an AWS backend stub, (6) README finally narrates ARCA/OSTIARIUS as the
plural, multi-backend story they were always meant to be.

"Done" means: an agent can ask "what secrets exist for the personal-sites project" and get back
names, purposes, and injection targets (never values); a human can point Portunus at a real GCP
project (`demo-project-483920` or `firefly-events-inc`), see what's already in Secret Manager
there, and register it with one command instead of hand-typing 19 references; and the GCP
backend authenticates keyless (WIF) instead of relying on ambient `gcloud` credentials.

## 2. What I Found

See `docs/research-brief.md` for the full detail. Highlights that shape the design:

- `Reference` (registry.py) already carries `provider`/`project`/`env`/`tags{}` — this epic
  *extends* it, it doesn't redesign it. `_STRUCTURED_TAG_FIELDS` is the boundary between
  "tag-matchable field" and "informational field" — new metadata (description/purpose) must
  stay out of that tuple; new injection-routing metadata should be tag-matchable.
- `GcloudBackend` is single-project, ambient-`gcloud`-credentials, one instance per CLI
  invocation. There's no concept of "the right backend/project for *this* reference" — `_build()`
  constructs one backend and the resolver uses it for everything.
- `origin/dos-81-keyless-wif` already solved the WIF token-exchange problem, tested, with the
  token never touching a log/repr/return value. It's 64 commits stale on the integration side
  but `auth.py` itself is portable close to verbatim.
- `resolve_by_tags` is fail-closed, single-match, and used by `find`/`ask`/`inject` today — it
  must not change shape. A browse/list query is a different contract (zero-to-many, no
  ambiguity failure) and needs its own method.
- `check_injectable` is already an allowlist (`enabled`/`locked` only) — any discovery-written
  reference that lands in `requested` state is automatically non-injectable with zero new code.
  This is the safety mechanism discovery leans on rather than reinventing.

## 3. My Proposed Approach

**Slice A — Richer metadata on `Reference`.** Add `description` (what it is), `purpose` (what
it's for), and `injected_as` (dict, `{env_name: adapter_target}` — e.g. `{"prod":
"env:STRIPE_KEY", "staging": "file:.env.staging"}`, describing *how* it gets injected per
environment without duplicating the full adapter config). All three default to `""`/`{}` and
migrate additively (`migrate_legacy_tags()` pattern) — existing `registry.json` files keep
loading unchanged. None of the three join `_STRUCTURED_TAG_FIELDS` (they're descriptive, not
identity) except that `find`/discovery output must surface them.

**Slice B — GCP backend selection per project + WIF auth.** Port `auth.py` from
`dos-81-keyless-wif` close to verbatim (`OIDCToken`, `EnvOIDCTokenSource`,
`GCPWorkloadIdentityAuth`, `AWSWebIdentityAuth`, `assert_no_long_lived_cloud_keys`) — it's
already tested against a fully mocked transport and needs no design change. Re-derive the
integration: `GcloudBackend` gains an optional `credential_provider` (mint-token-to-0600-tempfile
pattern from the branch, unchanged) and the CLI's `_build()` resolves the *project* per-request
from the `Reference.project` field (falling back to `PORTUNUS_GCP_PROJECT` for backward
compat) instead of one process-global project — a `GcpProjectBinding` concept (project id +
optional WIF audience) keyed by project name, read from a small new config surface
(`PORTUNUS_GCP_PROJECTS` env, JSON map of `project -> audience`, or per-project entries in a
new `gcp-bindings.json` under `PORTUNUS_HOME` — see Open Question 1). *(Grill H1)* That file
gets the same `0600` permission treatment `Registry` already promises for `registry.json` — it
can carry WIF audience strings (full resource names including project numbers and pool/provider
identifiers), which are infrastructure topology worth keeping non-world-readable even though
they're not secret values. `portunus auth gcp [--project]` mirrors the branch's `auth`
subcommand, mint-and-report-identity-only, never the token.

**Slice C — GCP discovery (read-only).** `portunus discover --provider gcp --project <id>`
calls `gcloud secrets list --project=<id> --format=json` (names + labels + create-time only —
`versions access` is never called by this path, structurally, because the discovery code has
no reference to a `SecretBackend` at all) and prints a diff against the current registry:
already-registered / not-yet-registered. `--register` writes not-yet-registered ones as new
`Reference`s in `state="requested"` (existing placeholder state — fails closed automatically),
`sm_name` set from the discovered name, `provider="gcp"`, `project=<id>`, `description` seeded
from the GCP secret's own `labels` if present (e.g. a `purpose` label), else empty. *(Grill U1)*
The local registry key (`Reference.name`, distinct from `sm_name`) is derived as
`<project>-<discovered-sm-name>` (lowercased, matching the existing `dostal-shared-*`/`demo-*`
naming convention) rather than the bare discovered name — two different GCP projects can
legally share a secret name, and the project prefix prevents a second discovery run from
silently colliding with an unrelated reference. If the derived name already exists pointing at
a *different* `sm_name`/`project` pair, `discover` skips that entry and reports it as a naming
conflict rather than overwriting — discovery never blind-overwrites an existing reference
regardless of its state. A human later runs `portunus retag`/edits metadata and flips state via
existing lifecycle commands — discovery never auto-enables anything.

**Slice D — LLM-facing "list keys for project" query.** `Registry.list_by_project(project,
*, provider=None, env=None) -> List[Reference]` — zero-to-many, metadata fields only, no
fail-closed single-match requirement (it's a browse, not a resolve; deliberately a different
method from `resolve_by_tags`, not an overload of it). `portunus ask "what secrets are
available for personalsites"` recognizes a new `intent_kind="list"` (alongside existing
`fetch`/`add`/`rotate`) and routes to this method, printing name/description/purpose/env/
injected_as for every match — never a value, and the return path never touches
`SecretBackend.access()` at all (same structural-safety pattern as discovery). A thin
`portunus list --project <id>` CLI command exposes the same method directly for scripting.

**Slice E — AWS backend stub.** `AWSSecretsManagerBackend` in `backend.py`, matches
`SecretBackend` protocol, `access()` raises `BackendError("AWS Secrets Manager backend is not
yet implemented — see portunus-vault-metadata design discussion")`. `AWSWebIdentityAuth` from
Slice B's ported `auth.py` stays available but unused by this stub — a future epic wires them
together. *(Grill V1)* `provider` has no enum/validation today (same free-string as `kind`/
`scope`) — nothing needs to make `"aws"` "legal." The actual fix is the failure mode: today an
unrecognized provider silently falls through `_build()`'s selection to whatever the default
happens to construct (`GcloudBackend`) and fails with a confusing GCP-flavored error against a
non-GCP secret; after this slice, `provider="aws"` routes to a real backend that fails clearly
and names itself.

**Slice F — ARCA/OSTIARIUS narrative + closeout.** README section rewrite: ARCA is explicitly
"pluggable backends behind one interface — local-encrypted, GCP Secret Manager (WIF), AWS
Secrets Manager (stub) — selected per-Reference by `provider`+`project`, not one global
choice," with a worked discovery example against `demo-project-483920`. OSTIARIUS section
gains the metadata-query surface (`list`/`discover`) alongside the existing boundary-resolve
description. CONTEXT.md gains the new vocabulary (`injected_as`, `list_by_project`,
`GcpProjectBinding`, discovery).

## 4. What Could Go Wrong

- **[high] Discovery accidentally calls a value-fetching gcloud command.** The entire safety
  case for Slice C rests on the discovery code path never holding a `SecretBackend` reference.
  Mitigation: discovery is implemented as its own small module with no import of
  `GcloudBackend.access` at all — not just "doesn't call it," structurally can't; a dedicated
  test asserts the discovery module's `gcloud` invocation only ever uses `secrets list`/
  `describe`, never `versions access`.
- **[high] A WIF-authenticated backend accidentally logs or returns the minted access token.**
  Same invariant class as a secret value. Mitigation: reuse the branch's already-tested pattern
  verbatim (temp file, 0600, unlinked in `finally`, token field `repr=False`) rather than
  reimplementing; port the existing `test_auth.py` assertions (token never in `repr()`, never
  in the return value of `mint()`'s stringification) as-is.
- **[medium] Real GCP calls (even read-only discovery) in the test suite would be flaky/slow/
  need live credentials in CI.** Mitigation: `discover` tests inject a `runner`/transport
  callable exactly like `GcloudBackend` already does for `subprocess.run` — no test hits real
  `gcloud`. Live-project discovery against `demo-project-483920`/`firefly-events-inc` is a
  manual smoke test only, never part of `pytest`.
- **[medium] Per-project GCP binding config (Open Question 1) adds a second place project
  config can live** (`PORTUNUS_GCP_PROJECT` env var still exists from before this epic).
  Mitigation: keep the legacy single env var as the fallback default project for backward
  compat; the new binding map is additive, not a breaking rename.
- **[low] `injected_as` metadata drifts from the real adapter config** (someone changes how a
  secret is actually injected without updating the metadata). Out of scope for this pass —
  it's documentation-grade metadata for the LLM query, not a config source the adapters
  actually read from. Flagged as a known limitation, not a blocker.

## 5. Dependencies and Constraints

- Slice A (metadata fields) is a prerequisite for D (list query) and C (discovery seeds
  `description`) — build first.
- Slice B (WIF backend + project binding) is independent of A but is a prerequisite for C
  (discovery uses the same project-binding concept to know which project to list).
- Slice D depends on A (needs the new fields to have something to show) and benefits from, but
  doesn't strictly require, C (can list manually-registered references before discovery ships).
- Slice E (AWS stub) is fully independent — can land any time.
- Slice F (docs) is last — narrates what actually shipped.
- `secret-boundary-invariant` and `audit-chain-integrity` apply throughout. This epic adds a
  second category to the boundary invariant: short-lived WIF credentials must never leak the
  same way secret values must never leak (see risk above) — worth stating explicitly in
  CONTEXT.md's terminology section during Slice F closeout.

## 6. Open Questions

1. Where does per-project GCP binding config (project → WIF audience) live? *(My call: a
   `PORTUNUS_HOME/gcp-bindings.json` file, parallel to `registry.json`, `0600` on disk
   (Grill H1) — carries WIF audience strings, which are infrastructure topology rather than
   secret values but still worth keeping non-world-readable; falls back to
   `PORTUNUS_GCP_PROJECT`/`PORTUNUS_GCP_WIF_AUDIENCE` env vars for a zero-config
   single-project setup, matching today's behavior exactly when no bindings file exists.)*
2. Does `list_by_project` belong on `Registry` or on a new module? *(My call: `Registry` — it's
   a read-only query over the same in-memory reference map `resolve_by_tags` already uses,
   same locking/loading story, no reason to split it out.)*
3. Should discovery ever *update* an already-registered reference's metadata (e.g. GCP label
   changed), or only report new ones? *(My call: report-only for drift on existing references
   this pass — silently rewriting a human-curated `description` from a GCP label would be
   surprising. `--register` only touches references that don't exist yet. Flag drift in
   `discover`'s output as a note, don't act on it.)*

## 7. Verification Strategy

```
VERIFICATION PLAN:
  Tools: pytest (existing), manual gcloud smoke test (demo-project-483920, firefly-events-inc)
  Automated: Reference metadata fields migrate additively (old registry.json still loads);
    GCP project binding resolves per-Reference; WIF token-mint path never logs/returns the
    token (ported test_auth.py assertions); discover module only ever invokes `secrets list`/
    `describe`, never `versions access` (structural + explicit test); discover --register
    writes state=requested (never enabled); list_by_project returns metadata only, zero-to-many,
    no value access possible from that code path; AWS backend raises BackendError, never a
    silent no-op or wrong-provider fallback.
  Manual: `portunus discover --provider gcp --project demo-project-483920` against the real
    project (read-only) to confirm real secret names/labels surface correctly; `portunus auth
    gcp` smoke test if a real WIF pool/provider is configured (may not be — acceptable to defer
    if no live WIF trust relationship exists yet, since the mint path is fully unit-tested
    against a mocked transport regardless).
  Not verifying: AWS backend behavior beyond "raises clearly" (fully out of scope, Slice E is
    intentionally a stub); injected_as being consumed by the adapter layer (documentation-grade
    metadata only this pass, per Open Question/risk above).
```

## 8. Scale Assessment

```
SCALE ASSESSMENT:
  Files affected: ~9 (registry.py, backend.py, new auth.py, new discover.py, cli.py,
    README.md, CONTEXT.md, tests for each)
  Subsystems: Registry (metadata), ARCA (backend selection + new AWS stub), new auth module,
    new discovery module, OSTIARIUS (CLI + list query)
  Migration required: additive only (new Reference fields default empty, new VALID provider
    value, no schema break)
  Cross-team coordination: no
  Unknowns: 1 (Open Question 1 — binding config location), already defaulted above, low-stakes

  RECOMMENDATION: Proceed to stories (skip H/V) -- every slice builds on an already-proven
    primitive (additive Reference fields, the existing requested-state fail-closed mechanism,
    resolve_by_tags's sibling-method pattern) or ports already-tested code (auth.py). No new
    architectural pattern is being invented, just a new backend + a new read-only query shape.
  RATIONALE: Medium scope by file count and subsystem count, but low architectural risk --
    real complexity is the porting/adaptation work (auth.py -> current backend.py shape),
    which is a well-scoped mechanical task, not a design uncertainty. H/V planning would mostly
    restate slice boundaries already made explicit above.
```
