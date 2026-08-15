# Design Discussion — portunus-bindings-settings-ui

## 1. Shape

Two independent extensions, both additive to existing, working UI/API shapes — no new page, no
new nav item, no new CLI command (both `portunus bindings set --account/--wif-audience` and
`portunus rotation-bindings set <provider> --account` already exist and are untouched):

1. **Vault-binding panel** (`ProjectExplorer.tsx`) gains two text fields — `account` and
   `wif_audience` — alongside the existing backend picker/sync-mode select, all still keyed to
   the currently-selected project. `POST /api/bindings` gains matching optional body fields.
2. **Rotation-binding account hint** (`DetailDrawer.tsx`) gains a small inline text field next
   to the existing Auto-rotate button, scoped to the currently-viewed reference's `provider`,
   editable only when the drawer is open for that reference (mirrors how rotation status is
   already fetched per-reference, not globally). `POST /api/rotation-status` (new — was GET-only)
   accepts `{provider, account}` and forwards to `portunus rotation-bindings set <provider>
   --account <value>` — never a `status` field (see research-brief.md §5).

## 2. Why extend existing panels, not build a new "Settings" page

The original framing (a first-pass fork report) proposed a whole new Settings tab. Rejected
after reading the real code: `backend`/`sync_mode` are ALREADY live, per-project, inside Project
Explorer's existing binding panel — adding `account`/`wif_audience` as two more fields on that
SAME panel is a strictly smaller, lower-risk change than building a parallel settings surface
that would duplicate the per-project selection UX Project Explorer already owns. Same reasoning
for rotation: `DetailDrawer` already computes and displays `rotationStatus` per-reference; adding
one editable field next to where that data already renders is smaller than inventing a
provider-keyed settings list elsewhere.

## 3. Is `account`/`wif_audience` safe to accept as free-text UI input?

Yes, on the same basis this codebase already documents. `VaultBinding.account` is a gcloud CLI
identity email (an identity *selector*, not a credential — the identity itself must already be
authenticated locally via `gcloud auth login`, which this UI does not do). `wif_audience` is a
WIF provider resource name (infrastructure topology, not a secret — `backend.py`'s own
`VaultBinding` docstring: *"a Workload Identity Federation provider resource name... not a
credential, but kept out of world-readable files anyway"*). Both are already returned in full by
`GET /api/bindings` today (confirmed live) — accepting them as UI input closes a write-path gap
that doesn't introduce any new read-path exposure. Neither ever appears in `RotationBinding` or
audit-log entries as a secret; both are string identity/topology fields the CLI already accepts
as plain args (`--account`, `--wif-audience`), so a UI text field carries the exact same trust
level as the CLI flag it replaces — no new boundary crossed.

## 4. `rotation-status` route: GET stays, POST is additive

`GET /api/rotation-status` is unchanged — still read-only, still feeds the Auto-rotate button's
disabled/tooltip state. The new `POST` handler mirrors `/api/bindings`'s own GET-after-POST
pattern (`portunus rotation-bindings set` then `portunus rotation-bindings show --json` to
return the fresh state) — same shape every other mutating route in this codebase already uses,
not a new pattern.

## 5. Fixing the stale `wif_audience` comment while touching this file

`ui/app/api/bindings/route.ts`'s existing comment claims *"the WIF audience value itself is
never returned by `bindings show`"* — verified false (research-brief.md §3). Corrected as part of
this epic's own edit to this file (the account/wif_audience POST support lands right next to
it), not as a separate unrelated fix — avoids leaving a known-wrong comment sitting next to new
code that directly contradicts it.

## 6. Self-grill

- *Does adding a free-text `wif_audience` field risk a user pasting something that looks like a
  credential into it by mistake?* A real UX risk for any free-text field near auth config. The
  field gets a placeholder/helper text making the expected shape explicit
  (`//iam.googleapis.com/projects/.../locations/global/workloadIdentityPools/.../providers/...`)
  and is visually distinct from a value-entry field (no password-style masking — masking here
  would *wrongly* imply it's secret, undermining the "this is topology, not a credential"
  framing this whole codebase already commits to). This is a UX mitigation, not a technical
  gate — the same posture the CLI's own `--wif-audience` flag already has (plain argv, no
  special handling), so the UI is not introducing a new risk the CLI didn't already carry.
- *Should the rotation-account field write on every keystroke, or only on save?* Explicit save
  (a button), not on-blur/debounced auto-save — matches `updateBinding`'s own click-to-set
  pattern (backend/sync-mode buttons), not a new interaction style.
- *What happens if `POST /api/rotation-status` is called for a provider with no existing
  binding?* `cmd_rotation_bindings_set` already defaults `status` to `"stub"` for a first-time
  provider (only-passed-fields-change pattern, same as `cmd_bindings_set`) — the route doesn't
  need to special-case creation vs. update, it's the same call either way.
- *Does this need a live GCP project to test?* No — every acceptance criterion is testable with
  `PORTUNUS_BACKEND=mock`/a scratch `PORTUNUS_HOME` and Playwright driving the actual dev server;
  no real cloud credential is required for either UI addition (unlike, say, real discover flows).

## 7. Scale assessment

**Small.** Two additive fields on one existing panel, one new POST handler mirroring an existing
route's own shape, one small inline field in an existing drawer section. No new page, no new nav,
no new CLI surface, no schema change (`VaultBinding`/`RotationBinding` already have every field
this exposes). `version_bump: minor` per this project's default (no breaking change, no reason
to deviate).
