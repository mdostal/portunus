# Design Discussion — portunus-vault-transfer

## 0. Goal

Let a second Portunus instance (a teammate's machine, a new agent's own `--home`, a fresh
install) gain real, working access to some or all of what a first instance's vault already
exposes — without hand-typing every `reg add`, and with real help getting actual *access*
(GCP auth/IAM) sorted, not just local bookkeeping copied over. Explicitly distinct from
`portunus vault export/import` (already ships full-vault, passphrase-locked, local-value
backup/restore — research-brief.md §1) and explicitly not bidirectional sync (already
deliberately deferred by portunus-vault-backup's own design-discussion §4).

## 1. Command surface: `portunus vault access export|import|verify`

Nested under `vault`, alongside the existing `export`/`import` (which keep their current
meaning — full local-vault backup). `access` names what this new set actually does: it doesn't
move secret values, it moves and verifies *access* to them.

- `portunus vault access export [--project P] [--org O] [--tags k=v,...] [--out path]` — a
  plain JSON bundle (never passphrase-locked, because it never contains a value — see §2),
  containing every matching `Reference`'s full metadata (`portunus reg json`'s own shape,
  reused not reinvented) plus every referenced project's `VaultBinding` and `RotationBinding`.
  No filter = everything, matching `reg json`'s own no-filter-means-everything convention.
- `portunus vault access import <bundle> [--force]` — recreates registry entries + bindings on
  the target instance (§3). Refuses a name collision with a *different* `sm_name`/`backend`
  unless `--force` (matching `vault import`'s existing `--force`-means-full-replace posture,
  scoped here to per-reference conflicts instead of whole-vault).
- `portunus vault access verify [--project P]` — attempts a real, boundary-safe fetch of every
  matching reference through the *target* instance's own now-configured backends, and reports
  per-reference readiness: reachable, needs `gcloud auth login`, needs an IAM grant (with the
  actual `gcloud` command to run), or needs a human `portunus drop` (local-only refs). Never
  fetches/prints/returns a value (§4).

## 2. Why the bundle is plain JSON, not passphrase-locked like `vault export`

`vault export`'s passphrase requirement exists because that archive contains `master.key` +
`vault.enc.json` together — enough to decrypt every stored value (portunus-vault-backup
design-discussion.md §2). This bundle never contains `master.key`, `vault.enc.json`, or any
other value-bearing file — only `Reference` metadata and `VaultBinding`/`RotationBinding`
config, all independently confirmed non-secret by their own docstrings (research-brief.md §2).
Locking a file that contains no secret behind a passphrase would be theater, not security —
and would incorrectly imply to an operator that the archive is more sensitive than it is.
`account`/`wif_audience` land in it unmasked, same posture the Settings UI already takes with
these exact fields (README.md ~line 570).

**Self-grill catch:** `description`/`purpose`/`tags`/`group` are free-text fields a human could
have put anything into. They're contractually non-secret (this whole codebase's `Reference`
schema has no value slot at all, and every existing export path — `reg json`, `crawl`, `report`
— already treats them as safe to print), but a human COULD paste something sensitive into a
free-text field by mistake, same risk `reg json`/`portunus report`/the leak-scan feature already
exist to catch, not a new risk this command introduces. No new mitigation needed here beyond
what already exists — noted so it isn't rediscovered as a false "gap" later.

## 2a. `resolved_backend` is captured at EXPORT time, on the source side — not re-derived blind on import

Which backend a reference actually resolves through is a 3-level precedence (`ref.backend`
override → project's `VaultBinding.backend` → the process's global `PORTUNUS_BACKEND`
fallback) that `cli.py::_make_backend_router` already implements correctly — but that
precedence depends on the CALLING instance's own global fallback, which the target instance
doesn't share with the source at import time. Re-deriving "is this local?" blind on the target
side would be wrong exactly when it matters most (an unscoped reference whose project has no
binding, relying on the source's global default). Fix: `export` computes each reference's real
`resolved_backend` using the SOURCE instance's own real backend router (the same one every
other command already builds via `_build()`) and bakes it into the bundle as one extra,
non-secret metadata field per reference. `import` then only ever READS that precomputed field —
never re-derives precedence itself. One real router implementation, called once, at the only
point (export time) where the answer is actually knowable.

## 3. Import mechanics — reuses `Registry.add()` directly, no registry.py changes

For each `Reference` in the bundle:

```python
target_state = ref.state
if bundle_entry["resolved_backend"] == "local":
    # The value lives ONLY on the source machine's vault.enc.json -- there is
    # no "access info" that can make it resolvable on a fresh instance. Land
    # it exactly the way an agent-initiated ask already does (registry.py's
    # own request() precedent) -- never silently claim it's ready to use.
    target_state = "requested"
registry.add(ref.name, ref.sm_name, state=target_state, **rest_of_ref_metadata)
```

`bundle_entry["resolved_backend"]` is the value §2a's export-time computation already baked in
— import never re-derives precedence, it only branches on that one precomputed string.
Every other state (`enabled`, `locked`, `dropped`, `revoked`) transfers unchanged for a
GCP/AWS-backed reference — importing the pointer really is sufficient once auth/IAM catches up,
no local value ever required.

**Collision handling:** if `name` already exists on the target with a *different* `sm_name` or
resolved backend, refuse (report the conflict) unless `--force`. An identical re-import
(same `sm_name`/backend) is always a safe no-op overwrite — re-running `import` after a
partial `export`/`verify` cycle must not require `--force` for the common "nothing actually
changed" case.

## 4. Verification — reuses `Resolver.resolve_call`'s existing boundary-safety, doesn't invent a new one

```python
def _verify_one(resolver, ref):
    try:
        resolver.resolve_call(f"{{{{secret:{ref.name}}}}}", boundary=lambda v: "reachable")
        return "reachable"
    except NotInjectable:
        return "needs `portunus drop <name> <sm_name> --stdin` (local-only, no value transferred)"
    except BackendError:
        return "needs auth/IAM -- try `portunus auth login <account>`, or an IAM grant on the GCP project"
```

This is a REAL fetch through the real backend — not a dry-run, not a metadata-only guess. The
boundary callable discards the value immediately (`lambda v: "reachable"`), the exact pattern
`test_boundary_receives_value_but_it_is_not_returned` already proves never leaks
(research-brief.md §4). `verify` is the literal answer to "portunus can already inject so it
should help get that going" — it uses the SAME injection machinery a real `resolve`/`ask`/`mcp`
call would use, just with a boundary that throws the result away instead of acting on it.

**Self-grill: does `verify` need its own MCP tool?** No, deliberately CLI-only, matching
`vault export`/`vault import`'s own existing precedent (design-discussion.md §6 in that epic:
"an archive containing every secret in the vault should never be triggerable by an LLM-facing
tool without a human directly initiating it"). `verify` doesn't touch a value's *content*, but
it DOES trigger real backend calls (GCP API calls, potential IAM error surfacing) across
potentially hundreds of references on a human's say-so — matching the same "a human initiates
this, not an agent on its own" posture, not because of value exposure but because of the
real-world side effects (API calls, potential quota/cost, revealing which specific IAM grants
are missing) an LLM shouldn't trigger unprompted.

## 5. Auth bootstrap — wire into existing `auth login`/`auth status`, don't rebuild them

`verify`'s "needs auth/IAM" hint names the specific `account` from the imported `VaultBinding`
so the operator can run `portunus auth login <that account>` directly — no new auth code, this
epic only threads the already-known account string into the hint message.
`cmd_auth_status`'s existing cross-reference logic (bindings vs. `gcloud auth list`) already
answers "am I authenticated for this account" — `verify`'s closeout story documents running
`portunus auth status` as the natural next step after `import`, before `verify`, rather than
duplicating that check inline.

**What Portunus explicitly cannot do, and doesn't pretend to:** grant the actual IAM permission
on the GCP project. That's a real gap between "instance B has the registry pointer" and
"instance B can actually read the secret" that only a human (or whoever administers that GCP
project's IAM) can close. `verify`'s job is to make that gap VISIBLE and actionable (the exact
`gcloud projects add-iam-policy-binding ...` command to run), never to paper over it or attempt
it — matching this codebase's consistent stance that Portunus never gets a write path into a
cloud provider's own access-control plane (same reasoning `portunus_drop`'s local-vault-only
scope already documents for GCP secret *creation*).

## Self-grill

- **What about local-encrypted-backend references that DO need to move (a teammate needs the
  same local-only secret)?** Out of scope here, and that's fine: `vault export`/`import`
  (whole-vault) already covers this today if the operator is willing to hand over the whole
  vault archive. A *selective* local-value transfer (encrypt just these N values for a specific
  recipient) is real, separable, materially different work (its own crypto/recipient-key
  design) — noted as a real, deliberately deferred follow-up, not silently dropped.
- **Why not fold `verify` into `import` (verify automatically after every import)?** Import
  should stay fast and side-effect-free beyond the registry/bindings write — verify makes real
  network calls (GCP API round-trips) that can be slow across hundreds of references and
  shouldn't block/couple to the import step succeeding. Kept as an explicit, separate,
  human-triggered step (§4's CLI-only reasoning applies here too).
- **Does `export`'s output ever need to be scoped by `repo` (not just project/org/tags)?** Yes —
  `--tags repo=<name>` already works today via the existing generic `--tags` filter (`reg.py`'s
  `matches_tag()` treats `repo` as a structured field, `resolve_by_tags`/`find` already support
  it) — no new flag needed, `--tags repo=event-api` is already the right spelling.

## Scale assessment

Medium: three new CLI subcommands (`vault access export/import/verify`), a new small module
(`vault_transfer.py`, matching `backup.py`'s own separate-module precedent for a distinct
concern) — the export/import mechanics reuse `Registry.add()`/`load_vault_bindings()`/
`save_vault_bindings()` directly (no registry.py/backend.py changes), and `verify` reuses
`Resolver.resolve_call()` directly (no resolver.py changes). No UI surface (matching `vault
export`/`import`'s own CLI-only precedent, §4's reasoning). `version_bump: minor` — new,
additive capability, no breaking change to any existing command.
