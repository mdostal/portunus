# Research Brief: portunus-vault-metadata

## Requirement

The user's own words: "we need more metadata -- i have to be able to ask it for a list of
keys available for a project as an LLM and get info back -- if i have SM in GCP and it has
all of the values, i need to know what it is, where it is going, what it is for -- use
firefly events from our GCP project and the personal sites one from mathew dostal on GCP --
both of those are SOLID examples AND we need to store the access of the different GCP tie in
-- so, let's get portunus to get the other parts we have described repeatedly in the long
term vision, the vault itself options and the api which is the door keeper (both had latin
names) for interacting and dealing with -- stub the vault stuff for AWS but right now we
implement with the same WIF and SM we have on GCP so that we have that but add more data and
metadata to know which key goes where and how and which one to inject per
environment/project/etc"

Decomposed:
1. Richer `Reference` metadata — what a secret is, what it's for, where it's injected per env.
2. An LLM/agent-facing "list keys for project X" query — names + descriptions, never values.
3. A real GCP Secret Manager backend using Workload Identity Federation (keyless), aware of
   multiple GCP projects (today's `GcloudBackend` takes one global project).
4. GCP secret *discovery* — enumerate what already exists in a live GCP SM project (names +
   labels/create-time only, never values) so it can be registered instead of re-created blind.
5. A stubbed-only AWS backend (matches `SecretBackend`, raises clearly, no real AWS calls).
6. Narrate ARCA (vault backends, plural) and OSTIARIUS (gatekeeper API) explicitly as the
   long-term multi-backend vision in README — the names already exist in code but the docs
   don't yet tell that story.

## Real infra confirmed this session (do not re-verify, use as given)

Local `gcloud` CLI is authenticated as `personal@example.com` (default project
`demo-project-483920`). Two live GCP projects are the worked examples:

- `demo-project-483920` ("PersonalSites") — Mathew Dostal's personal-sites project. 19 real
  secrets already exist in its Secret Manager (`AUTH_SECRET`, `RESEND_API_KEY`,
  `SANITY_API_*`, `dostal-shared-*`, etc.) — a solid non-empty discovery example.
- `firefly-events-inc` ("Firefly Events Inc") — reachable under the same account (cross-account
  IAM), Secret Manager currently empty (`Listed 0 items`) — a solid empty-project example.

A third gcloud account, `work@example.com`, is configured locally but its cached token is
expired and non-interactive reauth fails — do not assume it's usable without the user running
`gcloud auth login` themselves.

**Safety boundary for this epic:** any code path that talks to these real projects for real
must be read-only metadata (`gcloud secrets list` / `describe` — names, labels, create-time),
never `gcloud secrets versions access` (value fetch). Automated tests must not call live GCP at
all (mock the transport, exactly like the existing `MockBackend` pattern); live-project
exercise is a manual smoke test the operator runs interactively, same discipline used for the
UAT server standup earlier this session.

## Pre-existing keyless-WIF implementation to reuse (branch `origin/dos-81-keyless-wif`)

This repo's own remote already has tested, working code for exactly problem #3 above, 64
commits behind current `main` (predates the tag-schema/adapter/session work), so it can't be
merged/rebased cleanly, but is close to drop-in:

- `src/portunus/auth.py` (287 lines, new module): `OIDCToken` (frozen dataclass, `token` field
  `repr=False` so it never appears in logs/tracebacks), `OIDCTokenSource` protocol +
  `EnvOIDCTokenSource` (reads `PORTUNUS_OIDC_TOKEN[_FILE]`/`_ISSUER`/`_SUBJECT`/`_AUDIENCE`/
  `_EXPIRES_AT` — the harness supplies the token, Portunus never mints or stores one),
  `GCPWorkloadIdentityAuth.mint()` (STS `token-exchange` grant against
  `https://sts.googleapis.com/v1/token`, returns a short-lived `GCPAccessToken`, appends a
  `credential-mint` audit entry, never logs the access token), `AWSWebIdentityAuth.mint()` (same
  shape via `AssumeRoleWithWebIdentity`, kept even though the AWS *backend* itself stays
  stubbed — the auth-exchange code is already correct/tested and there's no reason to redo it
  later), and `assert_no_long_lived_cloud_keys()` (fails if `GOOGLE_APPLICATION_CREDENTIALS` /
  `AWS_ACCESS_KEY_ID` / `AWS_SECRET_ACCESS_KEY` are present, or a service-account JSON is found
  at a given path — a conformance check that the keyless invariant actually holds).
- `tests/test_auth.py` (125 lines): transport is fully injected/mocked (`transport=` callable
  param on both auth classes) — no real network calls, matches this epic's safety boundary
  exactly. Includes a `test_env_oidc_source_redacts_token_from_repr` style check that the token
  never leaks via `repr()`.
- The branch's `backend.py`/`cli.py` diffs show the integration shape: `GcloudBackend` grows a
  `credential_provider: GCPWorkloadIdentityAuth | None` constructor param; `access()` wraps the
  `gcloud` call in a `_access_token_file()` context manager that mints a token, writes it to a
  `tempfile.mkstemp` path chmod'd `0o600`, passes `--access-token-file=<path>` to `gcloud`, and
  unlinks the file in a `finally` block. `cli.py` wires this from
  `PORTUNUS_GCP_WIF_AUDIENCE` and adds a `portunus auth gcp|aws` subcommand that mints and
  prints only identity/scope/expiry, never the token itself.
- **Cannot reuse as-is**: that branch's `GcloudBackend`/`_build()` model one global project (no
  awareness of the current `provider`/`project`/`env` tag schema, `resolve_by_tags`, adapters,
  or `--home`). This epic must port `auth.py` close to verbatim and re-derive the backend/cli
  integration against the current `backend.py`/`registry.py`/`cli.py`, selecting the GCP project
  per-`Reference` (from its `project` field) rather than one process-global project.

## Current state (verified against code, this session)

- `src/portunus/backend.py`: `SecretBackend` protocol (`access(sm_name) -> str`), `MockBackend`
  (in-memory, `.set()`/`.access()`), `GcloudBackend(project="", timeout=30.0)` — shells out to
  `gcloud secrets versions access latest --secret=<name> [--project=<p>]` via `subprocess.run`,
  raises `BackendError` on missing CLI/timeout/non-zero exit, truncates stderr to 200 chars so a
  value can never leak through an error message. One backend instance per CLI invocation, one
  project.
- `src/portunus/registry.py` `Reference` dataclass (line ~48): `name`, `sm_name`, `scope`,
  `kind`, `state`, `approval`, `sm_path`, `provider`, `project`, `env`, `tags: dict`. No
  description/purpose/injection-target fields yet. `_STRUCTURED_TAG_FIELDS = ("provider",
  "project", "env", "scope", "kind")` — the set of fields `resolve_by_tags`/`matches_tag`
  recognize as structured (vs. falling through to the open `tags` dict). Any new structured
  metadata field (e.g. `description`) that should NOT be tag-matchable must stay outside this
  tuple; anything that should participate in `--tags key=value` lookups must be added to it.
  `migrate_legacy_tags()` is the existing additive-migration precedent — new fields must default
  to `""`/`{}` so old registry.json files keep loading.
- `Registry.resolve_by_tags(**partial_tags)` (registry.py) — fails closed via `NoMatch`/
  `AmbiguousMatch`, exact-match only (`matches_tag`), no substring/fuzzy matching. This is the
  natural site to extend for "list keys for project X" — a query that returns *all* matches
  metadata-only (not "resolve to exactly one or fail") is a new, additive method, not a change
  to `resolve_by_tags`'s existing fail-closed single-match contract (CLI's `find` command and
  `ask`'s fetch path both depend on that contract staying intact).
- `src/portunus/cli.py::find` (existing) already does tags -> metadata-only listing for a
  human, via `resolve_by_tags` semantics restricted to zero-or-one. An LLM-facing "list all
  keys for project X" is a *different* shape (zero-to-many, richer fields, intentionally no
  fail-closed single-match requirement since it's a browse, not a resolve) and should be its
  own command/method rather than overloading `find`.
- `src/portunus/broker.py::check_injectable` — allowlist (`state in ("enabled", "locked")`),
  already fail-closed for any new state; a discovered-but-not-yet-registered secret must never
  be injectable, so discovery should either not write to the registry at all (pure read/report)
  or write with `state="requested"` (existing placeholder state, already fails closed) pending
  human curation — not `state="enabled"`.
- README.md already documents ARCA (backend.py/localvault.py) and OSTIARIUS (resolver.py/
  cli.py) as component names but frames ARCA as "the GCP Secret Manager tier ... plus the
  local-encrypted tier" — singular/dual, not the plural multi-backend-with-metadata-registry
  story the north star describes. No AWS mention anywhere yet.

## Constraints (cross-cutting-concerns.yaml)

`secret-boundary-invariant` and `audit-chain-integrity` apply throughout, same as every prior
epic — doubly so here since this epic introduces real cloud credentials (short-lived WIF
tokens) as a second category of "value that must never leak" alongside secret values
themselves. `assert_no_long_lived_cloud_keys()` from the WIF branch is a concrete new
enforcement point worth carrying forward as its own tested guarantee, not just incidental to
porting `auth.py`.

## Scope decision for this pass

- GCP backend: implement for real (WIF-based, multi-project-aware). AWS backend: interface-only
  stub (`AWSSecretsManagerBackend.access()` raises `NotImplementedError` with a clear message);
  do not port `AWSWebIdentityAuth`'s *usage* into a working AWS backend this pass, only keep the
  already-tested auth-exchange class available in `auth.py` for a future epic to wire up.
- Discovery is read-only and opt-in (an explicit `portunus discover` command), never automatic,
  never a side effect of any existing command — it must not run during normal `resolve`/`ask`/
  `find` flows.
- "List keys for project" is metadata-only by construction (the method/command literally cannot
  reach a `SecretBackend.access()` call) — this makes the safety boundary structural, not just
  documented.
