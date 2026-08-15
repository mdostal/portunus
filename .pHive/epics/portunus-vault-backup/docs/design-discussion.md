# Design Discussion — portunus-vault-backup

## 1. Shape

`portunus vault export [--out path] [--passphrase]` bundles the real critical-state surface
(research-brief.md §2: `registry.json`, `master.key`, `vault.enc.json`, `vault-bindings.json`,
`rotation-bindings.json` if present, `audit.log`) into one archive, taken as a coordinated
snapshot under the SAME flock idiom `Registry`/`AuditChain`/`LocalEncryptedBackend` already use
(§3 below) — not four-plus independent unlocked reads. `portunus vault import <archive>
[--passphrase]` restores it, refusing to silently clobber an existing non-empty
`PORTUNUS_HOME` without an explicit `--force` (a real, hard-to-reverse action; matches this
project's own "confirm before destructive ops" posture used everywhere else — e.g. the desktop
app's relauncher never overwrites without verifying first).

## 2. The passphrase question — resolved

**Decision: the export archive is re-encrypted under an operator-supplied passphrase (PBKDF2-
derived key, same `cryptography` library already vendored — no new dependency), not the vault's
own Fernet key bundled as-is.**

Reasoning: `master.key` alone is sufficient to decrypt every value in `vault.enc.json` — it is
the single most sensitive artifact in the entire system, more sensitive than any individual
secret it protects. An export archive is, by definition, meant to leave the live, access-
controlled `PORTUNUS_HOME` (0700 dir, 0600 files) and land somewhere else — a backup drive, cloud
storage, a USB stick for machine-to-machine transfer. If that archive carries the live key
un-re-encrypted, the archive itself becomes exactly as sensitive as the original vault, but
without the original's OS-level file permission protections necessarily following it. This is
a different situation from the desktop app's ad-hoc-signing decision (design-discussion.md §5
of that epic) — that was "how much *distribution* security does a single-machine tool need,"
or a security-vs-convenience tradeoff. This is closer to the core secret-boundary invariant
this whole project exists to enforce: a value must never exist somewhere it can be read without
also passing whatever gate protects the original. An unprotected archive skips that gate
entirely. Requiring a passphrase (with a clear failure message on wrong/missing input, never a
silent fallback to no encryption) is the correct default, not optional hardening.

## 3. Coordinated snapshot, not independent reads

`export` acquires the SAME lock each of `Registry`/`AuditChain`/`LocalEncryptedBackend` already
uses on their own respective files (they already use *separate* lock files per component, not
one global lock) — so `export` acquires all three (registry.lock, vault.enc.lock, plus a fourth
for vault-bindings.json which currently has no lock at all, a small additive gap this epic closes
too) in a fixed order (alphabetical by path, to avoid a lock-ordering deadlock against any future
code that might acquire more than one of these) before reading any file, and releases all after
the archive is written. This guarantees the exported registry.json and vault.enc.json are from
the same consistent instant, not two reads straddling a concurrent writer's mutation.

## 4. The "sync" question — resolved, narrower than the original ask

**Decision, to confirm with the user (this is the one open question this design doc surfaces
rather than resolves outright):** ship export/import only in this epic. Do NOT build
bidirectional multi-machine sync. Reasoning: `SyncingBackend` (research-brief.md §4) already
solves the *specific* GCP-backed "keep working while disconnected, stay reasonably fresh"
problem for the references that need it — that machinery exists, is tested, and is unrelated to
whether the *local-only* vault has a backup story. A real bidirectional sync (two machines, both
locally-encrypted, both potentially mutated while the other was offline) needs a genuine
conflict-resolution design — per-reference last-write-wins? A merge that can silently overwrite
one machine's newer entry with another's stale one, exactly the class of bug this session spent
today finding and fixing in the concurrency-race fixes? That is real, separable, and materially
bigger work that deserves its own epic if it turns out to be needed, not a rider on "give me a
backup command." Export/import used manually (`export` on machine A, carry/upload the archive,
`import` on machine B) already covers "move to a new machine" and "recover from a wiped
machine" — the two concrete cases the user's own framing named (reinstall, machine move,
survive a fully-signed-app's stricter local sandboxing). Live, automatic, bidirectional sync is
a different, bigger ask that hasn't been separately confirmed as needed yet.

## 5. Self-grill

- *Does requiring a passphrase make the common "just back this up to my own encrypted disk"
  case annoying?* A real UX cost, acknowledged. Mitigated by making `--passphrase` accept a
  value via an env var (`PORTUNUS_EXPORT_PASSPHRASE`) for scripted/automated backup jobs, so it
  isn't necessarily an interactive prompt every time — the value still never appears in argv
  (same boundary-only discipline this project enforces everywhere else, mirroring how
  `portunus drop` never accepts a value via an inline flag).
- *What happens on `import` if the archive's passphrase is wrong?* Fails closed with a clear
  error, same posture as `LocalEncryptedBackend.access()`'s existing `InvalidToken` handling —
  never partially imports, never silently proceeds with corrupt data.
- *What about the legacy `gcp-bindings.json`?* Included in the export if present (it's small,
  and `load_vault_bindings()`'s own migration-safe fallback already depends on it existing when
  `vault-bindings.json` doesn't) — but not treated as an error if absent on a vault that's
  already migrated.
- *Should `import` require an empty target PORTUNUS_HOME, or support merging into an existing
  one?* v1: refuse to import into a non-empty PORTUNUS_HOME without `--force` (which does a full
  replace, not a merge) — a real merge (reconciling two registries with possibly-colliding
  names) is its own can of worms (the exact collision-detection complexity `Registry.retag()`
  already has to handle for a single mutation, multiplied across an entire second vault) and is
  explicitly deferred, not silently attempted.

## 4a. Resolved by the user: the real primary driver is eager sync-down on discover

Confirmed directly: the main workflow the user actually wants is *"add a new key to GCP SM, get
it registered in Portunus with a local cached copy, without a manual multi-step dance."* This is
mostly already-built machinery — `portunus discover --register` (creates the reference,
`state=requested`) and `SyncingBackend` (pulls a cached-mode project's references into the local
encrypted vault) both exist today. The gap: a freshly-registered reference under a
`sync_mode="cached"` project is only pulled into the local cache **lazily**, on first `access()`
— `discover --register` itself never touches a backend at all (by design, `discover.py` holds no
reference to any backend's `access()` method — a deliberate read-only guarantee). So "add a key
to SM" today means: `discover --register`, then separately `state enabled`, then the FIRST real
resolve is what actually triggers the local cache pull. The user wants that collapsed to one
step for the common case.

**New story this epic**: `discover --register` gains a `--sync-down` flag (or, resolved during
story-writing: becomes the default behavior when the target project's `VaultBinding.sync_mode
== "cached"`, since that's already an explicit per-project opt-in — a project that chose caching
presumably wants newly-discovered secrets cached too, not a second flag to remember). For each
newly-registered reference under a cached-mode project, immediately call the SAME
`SyncingBackend.access()` path a normal resolve would use — reusing the existing pull-cache
mechanism entirely, not inventing a second one. A registration under a non-cached or non-GCP
project is unaffected (nothing to eagerly pull). Still fails closed if the pull errors (network,
auth) — registration itself still succeeds (the reference exists either way), only the eager
cache-warm is best-effort, with a clear per-reference report of which ones warmed successfully.

Confirmed scope: **both** this story and export/import (§1-3) ship in the same epic — related
(both about "a local copy of what's really in the cloud"), but mechanically distinct enough to
be separate stories, not one conflated feature.

### 4b. A real gate interaction, checked directly, that shapes this story

`cmd_sync` (the existing `portunus sync <project>` command) calls `Broker.check_injectable(name)`
before syncing each reference, and **skips** any reference that isn't currently
`enabled`/`locked` (`NotInjectable` → `continue`). A freshly `discover --register`ed reference
always lands at `state="requested"` (fail-closed by design — a human must explicitly review and
promote it, so a placeholder never becomes silently usable). Reusing `cmd_sync`'s existing loop
as-is would mean the eager-warm silently does nothing for exactly the references it's meant to
help, since they're all `state=requested` at the moment they're created.

**Design decision:** the eager sync-down deliberately bypasses `check_injectable` for this one,
narrow, internal operation — calling the backend router's `.access()` directly (same call
`SyncingBackend` itself makes, same as `cmd_sync`'s own pattern of calling `.access()` purely
for its cache-populating side effect and never capturing/returning the value) to warm the local
cache, while the reference's `state` stays `requested` throughout. This does **not** weaken the
fail-closed guarantee: `check_injectable` still gates every real resolve/inject path
unconditionally — a cached-but-still-`requested` reference remains fully unresolvable through
`resolve`/`inject`/`ask`/every MCP tool, exactly as before. The only effect is that *when* a
human later reviews and promotes it to `enabled`, the first real resolve hits a warm cache
instead of triggering a live GCP fetch at that moment. Flagged explicitly here, and in the
story's own cross-cutting `secret-boundary-invariant` note, precisely because "bypass a security
gate" is the kind of decision that must be justified in writing, not silently done.

## 6. Scale assessment

**Small-to-medium.** One new coordinated-lock-acquisition pattern (real, but mechanical --
composes three existing locks + one new one), a passphrase-based re-encryption layer (uses the
already-vendored `cryptography` library, no new dependency), and CLI-only surface for v1 (no UI
work, no MCP tool -- an archive containing a passphrase-locked bundle of every secret in the
vault is not something an LLM-facing MCP tool should be able to trigger or receive a path to
without a human directly initiating it, matching the project's boundary-only posture). No sync
mechanism built -- explicitly scoped out per §4, pending user confirmation.
