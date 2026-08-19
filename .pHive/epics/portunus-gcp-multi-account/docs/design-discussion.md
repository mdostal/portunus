# Design Discussion: portunus-gcp-multi-account

## 1. What Are We Doing?

Fixing a real bug the user hit live: Portunus's GCP code paths follow gcloud's single mutable
"active account" instead of an explicit per-project identity, so authenticating a second GCP
account silently breaks access to projects governed by the first. The fix uses credentials that
are *already* locally available — `gcloud` already stores multiple credentialed accounts
simultaneously; every invocation already accepts `--account=<email>` to pick one explicitly.

"Done" means: two (or more) GCP identities can be authenticated locally at once, each project's
`gcp-bindings.json` entry says which one governs it, and both discovery and real value
injection use the right identity per project regardless of which account gcloud considers
"active" — plus a CLI command to actually configure this (today there is none).

## 2. What I Found

See `docs/research-brief.md`. Verified directly: `personal@example.com`'s credential was
never destroyed by logging into `work@example.com` — `gcloud projects list
--account=personal@example.com` works perfectly. The break is purely "no code path passes
`--account=`." Two real gaps found: `discover.py::list_gcp_secrets()` has no account parameter
at all (100% ambient-dependent), and there is no CLI command to write `gcp-bindings.json` —
only `save_gcp_bindings()`, called by tests, exists.

## 3. My Proposed Approach

**Slice A — `GcpProjectBinding` gains `account`.** Add `account: str = ""` alongside
`project`/`wif_audience`. `load_gcp_bindings()`/`save_gcp_bindings()` serialize it (additive —
an existing `gcp-bindings.json` without the field still loads, `account` defaults to `""`,
meaning "use ambient active account," today's behavior, unchanged).

**Slice B — `GcloudBackend.access()` passes `--account=`.** When a project's binding has a
non-empty `account` AND no WIF token is being used for that call (WIF and `--account` are
mutually exclusive — a minted access token already carries identity; `--account` only matters
for the ambient-auth fallback path), add `--account=<binding.account>` to the gcloud
invocation. WIF stays preferred when configured; `account` is the practical fallback for
today's real projects, none of which have real WIF trust yet.

**Slice C — `discover.py::list_gcp_secrets()` gains an `account` param.** Mirrors Slice B:
`list_gcp_secrets(project, account="", runner=None, timeout=30.0)` adds `--account=<account>`
to the `gcloud secrets list` command when given. `cmd_discover` (cli.py) looks up the project's
binding and passes its `account` through — this is the exact code path that broke live.

**Slice D — `portunus bindings set/show` CLI command.** The missing piece: without a way to
configure `gcp-bindings.json`, this epic's fix is unusable except by hand-editing JSON.
`portunus bindings set <project> [--account EMAIL] [--wif-audience AUDIENCE]` (upserts one
binding, preserving the field not passed), `portunus bindings show [<project>]` (prints one
binding or all, including the real `account`/`wif_audience` values). *(Grill U1)* This is
intentionally a different bar than the UI's `wif_configured` boolean-only rule
(portunus-vault-metadata-ui) — that rule was about not exposing infrastructure topology over a
*browser-rendered* surface (screen-sharing/accidental-exposure risk). `portunus bindings show`
is a local CLI reading the operator's own `gcp-bindings.json` (already a `0600` file they can
`cat` themselves) — same trust boundary, not a new exposure. Only minted tokens/values remain
the hard boundary everywhere.

**Slice E — Closeout.** Verify multiple accounts really work concurrently: discover
`demo-cicd` with `account=work@example.com` and `demo-project-483920` with
`account=personal@example.com` in the same process/session, back to back, both succeeding
regardless of gcloud's ambient active-account state. README/CONTEXT.md, version bump,
CHANGELOG.

## 4. What Could Go Wrong

- **[high] `--account` and `--access-token-file` (WIF) both being added to the same gcloud
  invocation would be nonsensical** (conflicting identity signals). Mitigation: mutually
  exclusive by construction — `--account` is only appended in the `else` branch where no WIF
  token file was minted for that call, not as an independent flag.
- **[medium] `portunus bindings set` could accidentally clobber the other field** (e.g. setting
  `--account` wipes a previously-configured `--wif-audience`). Mitigation: `set` reads the
  existing binding first and only overwrites fields explicitly passed on that invocation — an
  upsert, not a replace, mirroring `Registry.retag()`'s own "only passed fields change" pattern.
- **[low] A user could pass an `--account` email that isn't actually locally authenticated.**
  Mitigation: out of scope to validate against `gcloud auth list` at write time (that's a
  runtime concern, not a config-write concern) — the resulting `gcloud` invocation will fail
  with gcloud's own clear "no such credentialed account" error at actual access time, which is
  sufficient; Portunus doesn't need to duplicate gcloud's own validation.

## 5. Dependencies and Constraints

- Slice A is a hard prerequisite for B, C, D.
- Slices B and C are independent of each other once A lands.
- Slice D depends on A (needs the field to upsert).
- Slice E runs last, depends on all.
- `secret-boundary-invariant`/`audit-chain-integrity` — `account` is an identity-selector
  string (an email address), not a credential; no new value-adjacent surface.

## 6. Open Questions

None outstanding — the user's own live-tested finding (both accounts still locally
credentialed, `--account=` already proven to work) resolved the design directly; no fork
remained to ask about.

## 7. Verification Strategy

```
VERIFICATION PLAN:
  Tools: pytest, manual verification against the real local gcloud multi-account credential
    store (both real accounts already authenticated this session)
  Automated: GcpProjectBinding round-trips account through load/save_gcp_bindings; GcloudBackend
    .access() appends --account= only when no WIF token file is used for that call; discover's
    list_gcp_secrets() appends --account= when given; portunus bindings set upserts without
    clobbering the other field; portunus bindings show never prints a WIF audience mixed with
    a value (there is no value in scope here) but does print account/audience presence.
  Manual: portunus bindings set demo-cicd --account work@example.com; portunus bindings set
    demo-project-483920 --account personal@example.com; portunus discover --project
    demo-cicd (real, live) followed immediately by portunus discover --project
    demo-project-483920 (real, live) in the same shell session, both succeeding regardless of
    gcloud's current ambient active account.
  Not verifying: WIF mechanics themselves (unchanged, already covered by portunus-vault-
    metadata's tests); any UI (explicitly deferred to the settings-page epic).
```

## 8. Scale Assessment

```
SCALE ASSESSMENT:
  Files affected: ~5 (backend.py, discover.py, cli.py + tests)
  Subsystems: ARCA (GCP backend), discovery module, CLI
  Migration required: no (additive field, defaults to today's ambient behavior)
  Cross-team coordination: no
  Unknowns: 0

  RECOMMENDATION: Proceed to stories (skip H/V) -- small, well-understood, direct fix for a
    concretely reproduced bug; every slice is additive to already-proven patterns.
  RATIONALE: Small-to-medium scope, low risk, high real-world value -- gates the user's next
    milestone directly.
```
