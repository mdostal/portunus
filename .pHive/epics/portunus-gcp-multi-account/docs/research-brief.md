# Research Brief: portunus-gcp-multi-account

## Requirement

User hit a real bug live: running `gcloud auth login` to authenticate `work@example.com`
silently deactivated `personal@example.com` for any ambient `gcloud` command — because
`gcloud`'s CLI has a single mutable "active account" pointer, and every Portunus GCP code path
(`GcloudBackend.access()`, `discover.py::list_gcp_secrets()`) shells out to bare `gcloud`
commands with no explicit identity, so they silently follow whichever account happens to be
active at call time. User's words: "portunus needs to be able to host and have multiple under
the covers and link them correctly and integrate -- if all of that is there and done and we can
start the SM injection through portunus, i'll plug you into the claude console..." — this
explicitly gates their next milestone (a real formal-release test session).

## Verified: this is fixable without any new GCP-side setup

```
$ gcloud auth list
     Credentialed Accounts
ACTIVE  ACCOUNT
        personal@example.com
*       work@example.com

$ gcloud projects list --account=personal@example.com --format="table(projectId,name)"
PROJECT_ID                      NAME
firefly-events-inc              Firefly Events Inc
...  <works perfectly -- the credential is fully intact>
```

`personal@example.com` was never logged out at the credential-store level — `gcloud` already
holds multiple credentialed accounts simultaneously; only the *default* identity used by a
command with no explicit `--account=` flag changed. Every real `gcloud` invocation already
accepts `--account=<email>` to select which locally-stored credential to use, per call,
independent of the global "active" pointer. This is the whole fix: make Portunus's GCP code
pass `--account=` explicitly per project-binding instead of depending on ambient state.

## Current state (verified against code)

- `GcpProjectBinding` (backend.py, from portunus-vault-metadata) has `project: str` +
  `wif_audience: str` only — no `account` field.
- `GcloudBackend.access()` already resolves a *WIF* credential per project via
  `_credential_provider_for()`, but WIF requires a real GCP-side workload-identity-pool trust
  relationship, which does not exist yet for any of the user's real projects (`wif_configured`
  has read `false` for every project checked this session). WIF is the eventual keyless
  destination; ambient multi-account `--account=` selection is the immediate, already-available
  fix using credentials that already exist locally.
- `discover.py::list_gcp_secrets(project, runner=None, timeout=30.0)` has **no account
  parameter at all** — it always shells `gcloud secrets list --project=<p> --format=json` with
  zero identity selection, 100% dependent on ambient active-account state. This is the exact
  code path that broke when the user switched accounts.
- **No CLI command exists to write `gcp-bindings.json` at all.** `save_gcp_bindings()`
  (backend.py) is only ever called by tests today — a human has no way to configure a
  project's binding (WIF audience or, after this epic, account) without hand-editing JSON.
  This is a real gap this epic must close, not just a schema extension.
- `cmd_discover` (cli.py) calls `list_gcp_secrets(args.project)` with no account -- needs to
  look up the project's binding and pass its account through once one is added.

## Constraints (cross-cutting-concerns.yaml)

`secret-boundary-invariant` — an `--account=` flag selects *which already-locally-authenticated
identity* runs a command; it is not a credential itself (no token/password value flows through
it), same class as `--project`. `audit-chain-integrity` — no new audit entries needed;
`bindings set` is registry-adjacent config, not a secret-access event.

## Scope decision for this pass

- Only the ambient-`--account=` mechanism is built this pass (uses what's already locally
  authenticated). WIF remains the already-shipped keyless path for when real trust
  relationships exist later — this epic doesn't touch WIF's own mechanics, only adds the
  practical alternative for today's real accounts.
- A `portunus bindings` CLI command group (`set`/`show`) is in scope — without it, this epic's
  fix would be unusable (no way to actually configure which account governs which project).
- No UI surface this pass — binding management (both WIF audience and account) is UI work that
  belongs with the separate settings-page epic already flagged as a follow-up; this epic is
  CLI/backend only, matching the user's own priority order ("if all of that is there and done
  ... I'll plug you into the claude console").
