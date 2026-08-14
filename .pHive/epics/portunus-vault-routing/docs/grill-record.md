# Grill Record — portunus-vault-routing

**Source draft:** .pHive/epics/portunus-vault-routing/docs/design-discussion.md
**CONTEXT.md substrate:** present
**inconsistency_risk_signals:** research-brief §"The real design questions", §"inconsistency_risk_signals"
**round_number:** 1
**unresolved_count:** 4
**Generated:** 2026-08-14T02:00:00Z

## Summary

- Vocabulary mismatches: 1 finding
- Hidden assumptions: 2 findings
- Unresolved tensions: clean
- Convention violations: 1 finding
- Posture mismatches: clean

## Vocabulary mismatches

- **H4** — `VaultBinding.backend` (Slice A) and the existing `Reference.provider` field (a
  free-text tag like `"vercel"`/`"gcp"`/`"github"`, used for display and tag-matching since
  portunus-vault-metadata) are never distinguished in the draft, and their names are close
  enough to invite conflation. They answer different questions: `provider` is *who issued* the
  secret (a Vercel-issued key can be stored in GCP Secret Manager); `VaultBinding.backend` is
  *where the value physically lives* (which adapter Portunus calls). A reference with
  `provider="vercel"` in a project bound to `backend="gcp"` is the normal case, not a
  contradiction — but nothing in the draft says so.
  - Draft location: §3 Slice A
  - Resolution: add one explicit sentence to Slice A distinguishing the two fields by the
    question each answers, so a future reader (including mid-implementation me) doesn't conflate
    "where it's issued" with "where it's stored."

## Hidden assumptions

- **H1** — Slice C's `SyncingBackend` writes cached remote copies into
  `LocalEncryptedBackend`'s existing flat `{sm_name: encrypted_value}` store — the *same*
  namespace `portunus_drop` already writes genuinely-local secrets into. Two references with
  different registry `name`s could share an `sm_name` (e.g. a locally-dropped secret happens to
  reuse a GCP secret's own name) and silently collide in local storage — whichever one synced or
  stored last wins, no error, no warning. This risk exists in principle today too (any two
  references sharing an `sm_name` already collide), but this epic makes it *materially* more
  likely by starting to write GCP-originated names into the same store genuinely-local secrets
  use, for projects that never previously touched the local backend at all.
  - Draft location: §3 Slice C
  - Why this matters: a silent collision means one project's cached value quietly overwrites an
    unrelated local secret's value with no signal to the user.
  - Resolution: `SyncingBackend` namespaces its local-store key as `f"{project}:{sm_name}"`
    (project-prefixed) rather than reusing `sm_name` bare — this cannot collide with a directly-
    `portunus_drop`-ped secret unless someone deliberately names their own `sm_name` with a colon
    matching that exact pattern, which is easy to guard against separately if it ever matters.
    State this explicitly in Slice C rather than leaving the namespace unspecified.

- **H2** — The draft's router fallback ("no binding matches `ref.project` → falls back to the
  single globally-selected backend from `PORTUNUS_BACKEND`") doesn't address what happens when
  `PORTUNUS_BACKEND=mock` **and** a `vault-bindings.json` with real `backend="gcp"` entries
  happens to be present (e.g. a stray file in a test's `PORTUNUS_HOME`, or a real developer
  running `mock` mode against their real `PORTUNUS_HOME` for a dry run). As drafted, a matching
  binding would win and the router would try to construct a *real* `GcloudBackend` even though
  the caller explicitly asked for mock mode — surprising and, in a test context across this
  session's ~300 existing tests, a real correctness/isolation risk if any test's fixture
  directory ever accumulates a `vault-bindings.json`.
  - Draft location: §3 Slice B
  - Why this matters: `PORTUNUS_BACKEND=mock` is a safety rail (tests, dry runs) — it must never
    be silently overridden by config file content.
  - Resolution: `_build()` constructs the router **only** when `PORTUNUS_BACKEND` is unset or
    `local`/`gcloud`/`aws` (today's real modes). When `PORTUNUS_BACKEND=mock`, skip router
    construction entirely — every reference resolves through the single `MockBackend`, exactly
    as today, regardless of any `vault-bindings.json` content. State this explicitly in Slice B.

## Convention violations

- **H3** — Every other cross-cutting-concern note in this session's epics states the boundary-
  invariant implication explicitly, even when the answer is "unchanged" (e.g.
  portunus-local-create's story 02 says "N/A -- pure metadata operation" rather than staying
  silent). §3 Slice C never states whether a cached copy sitting in the local vault is held to
  the same at-rest protection as a genuinely-local secret. It is (same `LocalEncryptedBackend`,
  same Fernet encryption) — but the draft leaves this implicit rather than confirmed.
  - Draft location: §3 Slice C
  - Resolution: add one explicit sentence: cached copies are encrypted at rest identically to
    directly-dropped local secrets — `SyncingBackend` introduces no new plaintext-at-rest
    location, it reuses `LocalEncryptedBackend.store()` unchanged.

## Notes

All four findings are real precision gaps in an otherwise sound architecture, not open product
questions — each has a concrete, low-cost resolution folded into the revised draft. None changes
the slice structure or story count.

## Out of scope (this pass)

Grill does NOT propose solutions, score quality, gate work, or prioritize findings.
