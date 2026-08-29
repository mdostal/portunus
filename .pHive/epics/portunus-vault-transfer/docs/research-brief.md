# Research Brief — portunus-vault-transfer

## 1. The ask, and what it's NOT

User's own framing: "we need a way to be able to have an import export / copy / or transfer of
vault access info between portunus instances -- portunus can already inject so i think it
should be able to help setup and get that going as well or oauth it etc."

**This is not `portunus vault export/import`, which already ships** (`backup.py`, `cmd_vault_
export`/`cmd_vault_import`, portunus-vault-backup epic). That command is a coordinated,
passphrase-locked snapshot of the *entire* local-encrypted vault's critical-state surface --
`registry.json`, `master.key`, `vault.enc.json`, `vault-bindings.json`, rotation-bindings,
`audit.log` -- all-or-nothing, whole-vault, for backup/restore or moving to a new machine as
the *same* logical vault. It genuinely solves "back up my vault" and "move my vault to a new
machine." It does not solve "let a second, independent Portunus instance gain working access to
some or all of what a first instance already has."

**This is also not bidirectional multi-machine sync**, which the portunus-vault-backup epic's
own design-discussion §4 explicitly considered and deferred: "A real bidirectional sync (two
machines, both locally-encrypted, both potentially mutated while the other was offline) needs a
genuine conflict-resolution design... That is real, separable, and materially bigger work that
deserves its own epic if it turns out to be needed, not a rider on 'give me a backup command.'"
This epic is not that epic -- no live sync, no conflict resolution, no two-way merge.

**What the user is actually asking for**, read against the real architecture: a way for a
*second* Portunus instance (a teammate's machine, a new agent's own `--home`, a fresh install)
to become able to `resolve`/`inject` some or all of what a *first* instance's vault already
exposes -- without hand-typing dozens/hundreds of `reg add` commands, and with help getting the
actual *access* (GCP IAM, WIF, `gcloud auth login`) sorted, not just the local bookkeeping.

## 2. What "vault access info" actually is, for most of this vault

`Reference` (registry.py) never carries a value -- only metadata (name, sm_name, org, project,
env, tags, description, purpose, group, related, backend, repo, source_files). For a
GCP-backed reference (the majority of this vault's real, `state=enabled` entries --
`personalsites-487021-google_generative_ai_api_key` et al.), the *value* lives in GCP Secret
Manager, not locally at all. The only things a second instance actually needs to reach that
same value are:

1. The registry entry itself (`reg add` with the same `sm_name`/`project`) -- pure metadata,
   already exportable via `portunus reg json`.
2. The project's `VaultBinding` (`backend.py`) -- `backend`/`sync_mode`/`account`/
   `wif_audience` -- also pure metadata. `backend.py`'s own `VaultBinding` docstring calls
   `wif_audience` "infrastructure topology, not a credential"; `README.md` (~line 570) makes
   the same call for `account`: "Neither field is a credential (account is an identity
   *selector*; the identity itself must already be authenticated locally via `gcloud auth
   login`)" -- two independent confirmations of the same fact, not one source paraphrased twice.
3. A real, authenticated GCP identity on the *target* machine with IAM read access to that
   project's secrets -- `portunus auth login <email>` (thin `gcloud auth login` wrapper) +
   `portunus auth status` (cross-references bindings against `gcloud auth list`) already exist
   for this, but the actual IAM grant itself is a GCP-side action outside anything Portunus can
   perform (Portunus has no write path into GCP IAM policy, by design, same reasoning that
   already keeps `portunus_drop`'s cloud-side write path unbuilt).

**Verified against source:** `Registry.add()` (registry.py) already accepts a full `state`
parameter and every metadata field in one call -- no registry.py changes are needed to
reconstruct a reference on a target instance; the import mechanism is orchestration over an
existing, complete API. `VaultBinding`'s fields (backend.py:63-83) are confirmed non-secret
by the class's own docstring, matching this brief's claim independently.

## 3. The one real gap: local-encrypted-backend references

For a reference whose backend is `local` (no GCP/AWS project backs it -- the value lives only
in this ONE machine's `vault.enc.json`), there is no "access info" that can make a second
instance able to resolve it -- the value doesn't exist anywhere the second instance can reach.
Recreating such a reference on the target must land it in a state that's honest about this,
never a silent lie that it's ready to use. `Registry.request()` already exists for exactly this
shape of claim ("a value-less placeholder... state=requested fails closed via
Broker.check_injectable exactly like dropped/revoked") -- built for agent-initiated asks, but
the state semantics are identical to what's needed here: a human must `portunus drop` the real
value on the target before it's usable. `Registry.add(..., state="requested")` is the
mechanism (`request()` is a thin wrapper around the same state, missing several metadata fields
`add()` accepts directly).

## 4. Verification without ever touching a value

The user's own words: "portunus can already inject so i think it should be able to help setup
and get that going as well." Read literally: after import, the operator wants confirmation that
each transferred reference actually works on the new instance -- not just that a registry
pointer exists.

**Verified against source, a safe technique already proven elsewhere in this codebase:**
`Resolver.resolve_call(template, boundary)` (resolver.py) fetches the real value and passes it
to a caller-supplied `boundary` callable, returning ONLY the boundary's own return value --
never the value itself. `test_boundary_receives_value_but_it_is_not_returned` (test_resolver.py)
proves this directly. A verification command can call `resolve_call` with a boundary that
simply returns `"reachable"` (never touching, printing, or returning the real value) to prove
an end-to-end fetch actually succeeds through the real backend -- structurally identical
boundary-safety to every other real injection path, not a new invariant.

On failure, `BackendError` (raised by `GcloudBackend.access` on a real IAM/auth problem) and
`NotInjectable` (raised by `check_injectable` on a `state=requested` local-only reference) are
both already real, already-tested exceptions this verification command needs only to catch and
translate into an actionable hint -- `portunus auth login <email>` for the former,
`portunus drop <name> <sm_name> --stdin` for the latter -- reusing hint-string patterns
`check_injectable`'s own `NotInjectable` message already establishes (broker.py's `hints` dict,
`"requested": "a human must fulfill it via \`portunus drop\`"`).

## 5. Scope line vs. `portunus_discover`

`portunus_discover`/`portunus discover` (discover.py) already does registration in the OTHER
direction: read-only enumeration of what already exists in a *live GCP project*, optionally
registering not-yet-registered secrets as `state=requested` placeholders. That's "make this
instance's registry match a live cloud project it already has IAM access to." This epic is
different: "make a SECOND instance's registry match a FIRST instance's registry" -- the source
of truth is another Portunus instance's own registry + bindings, not a live cloud enumeration.
Genuinely complementary, not overlapping: `discover` for "sync registry to cloud reality,"
this epic for "hand a colleague/second instance the same map another instance already has."
