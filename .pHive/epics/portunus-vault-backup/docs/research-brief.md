# Research Brief — portunus-vault-backup

## 1. Ask

User: *"we'll need a way to backup and do the write up at some point because anything that
becomes a fully signed app for the local ones will have to go -- and we need to ensure we get
the repo and the use case for it and whatnot..."* then, on scoping: *"let's start the hive plan
/plugin-hive:plan portunus vault export/import and sync"*.

Two distinct concerns raised, not yet confirmed as one feature (see §4):
- **Backup/restore** — a point-in-time portable snapshot, so a vault isn't a single-machine
  single-point-of-failure.
- **Sync** — named explicitly, distinct from backup. Likely multi-machine live consistency, but
  not yet confirmed to mean that specifically (see design-discussion.md's open questions).

## 2. The real backup surface, checked directly — broader than initially assumed

`ls -la ~/.portunus/` (the real state home, `paths.py::home()`):

```
.clock                 -- audit seq counter (small, but see §3 on lock files below)
.clock.lock            -- LOCK FILE, not state -- must never be backed up/restored
approvals/              -- time-boxed approval tokens -- ephemeral, excludable
audit.log               -- tamper-evident hash chain (143KB on the real vault)
gcp-bindings.json       -- LEGACY vault-bindings format, still present alongside the new one
master.key              -- the Fernet symmetric key -- CRITICAL
registry.json           -- reference registry (244KB, 385 real references) -- CRITICAL
registry.lock           -- LOCK FILE -- must never be backed up/restored
sync-state.json         -- SyncingBackend's cache-recency markers -- regeneratable, not critical
vault-bindings.json     -- per-project backend/sync_mode/account/WIF config -- CRITICAL
vault.enc.json          -- encrypted secret values (2KB local-only entries) -- CRITICAL
vault.enc.lock          -- LOCK FILE -- must never be backed up/restored
```

**Correction to the original framing** (this session's own earlier recommendation, offered
before checking): backup is NOT just `master.key` + `vault.enc.json`. `registry.json` is
equally critical — losing it while keeping the encrypted vault means every value becomes
unreachable (no name to look it up by; `LocalEncryptedBackend.access()` keys strictly on
`sm_name`). `vault-bindings.json`/`rotation-bindings.json` matter too — without them, a restored
vault's GCP-backed references silently fall back to whatever `PORTUNUS_BACKEND` happens to be
set to, not the per-project routing the user actually configured (backend.py's 3-level
precedence, docs/architecture.md §2). The three lock files must be explicitly excluded — a
restored stale `.lock` with 0 bytes is harmless on its own (flock state isn't file content), but
including it invites confusion about what's "real" state versus incidental.

## 3. Concurrency, confirmed relevant here too

Both fixes shipped earlier today (`AuditChain._locked()`, `LocalEncryptedBackend._locked()`,
`Registry._locked()` already existing) establish the flock idiom this codebase uses for
serializing mutation across processes. A backup/restore operation reading multiple files
(registry.json + vault.enc.json + master.key + vault-bindings.json, at minimum) is itself a
multi-file read that could observe an inconsistent snapshot if it races against a live writer —
e.g. reading registry.json after a new reference was added but vault.enc.json before that
reference's value was stored. This needs its own consideration in design (a coordinated
snapshot, not four independent unlocked reads) — not the same bug already fixed, but the same
class of risk, in a new surface.

## 4. Existing "sync" prior art in this codebase — real, but a different question

`SyncingBackend` (backend.py, portunus-vault-routing epic) already does something called "sync"
today: a recency-aware, **pull-only, GCP → local cache** — it copies FROM GCP Secret Manager
INTO the local encrypted vault as a read-through cache, never the reverse, and never for
non-GCP-backed references. This is NOT the same problem as "keep two independent local vaults
on two machines consistent with each other" — that would need bidirectional sync with real
conflict resolution (what happens when the same reference is modified on two machines while
offline?), which `SyncingBackend`'s one-directional pull model doesn't address at all. Whether
"sync" in the user's ask means (a) this existing pull-cache pattern is already sufufficient for
their actual need, (b) a manual export/import round-trip between machines (no live
sync, no conflict resolution — just "point machine B at machine A's most recent backup"), or
(c) genuine bidirectional live multi-machine sync, is the single most consequential open
question this brief surfaces — not something to guess at (design-discussion.md's grill section
addresses this directly, and it should be confirmed with the user before finalizing stories).

## 5. Scope for this epic

**In scope for a v1 confirmed as valuable regardless of the sync-scope answer above:**
`portunus vault export`/`portunus vault import` — a portable, atomically-consistent snapshot of
the real critical-state surface (§2), with the lock/ephemeral files correctly excluded.

**Genuinely open, needs a user decision before story-writing:** whether "sync" ships as (a) manual
export/import only, informally used as ad-hoc backup/restore/relocate, (b) documented reliance on
the existing SyncingBackend pull-cache pattern (already shipped, nothing new to build for GCP-
backed references specifically), or (c) a new, materially bigger bidirectional sync mechanism —
deferred to a future epic if chosen, given the real conflict-resolution design work it would need.

**Also open:** whether the export archive should be re-encrypted under an operator-supplied
passphrase for safe out-of-band storage/transport, or whether bundling the existing Fernet key
as-is (protected only by wherever the archive itself is stored) is acceptable for v1 — a real
security-design call, not a mechanical one (design-discussion.md §2 grills this directly).
