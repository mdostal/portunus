# Research Brief — portunus-leak-scan

## 1. The ask, as given

Mid-stream during portunus-vault-trust-and-access, explicitly deferred until that epic (and its
follow-on, portunus-metadata-crawl) fully shipped:

> "portunus will need a set of tools for checking, validating, and ensuring logs, .claude,
> conversation files, etc didn't get or leak a key and if they did it warns and slowly escalates
> the need for rolling the key"

Three parts: (a) detect whether a managed secret's actual value shows up somewhere it shouldn't
(logs, `.claude` conversation transcripts, other local files), (b) warn when it does, (c)
escalate that warning over time until the secret is rotated.

## 2. What already exists — verified against the real code, not assumed

- **`Backend.access(sm_name, project="") -> str`** (`backend.py`) is the one call every backend
  (local/GCP/stub AWS/stub Azure) implements to return a decrypted value. `cli.py`'s
  `_eager_sync_down()` shows the established pattern for getting a value transiently via
  `resolver.backend_for(ref)` + `backend.access(...)` without it escaping the function it's
  fetched in.
- **`AuditChain.append(action, secret, result, ...)`** (`audit.py`) already establishes the
  discipline this epic must extend to a new surface: `secret` is always a *name*, never a value.
  Every existing audited action (`resolve`, `gate`, `grant`, `drop`) follows this; leak-scan
  findings must too.
- **`Broker.check_injectable()`** (`broker.py`) is the fail-closed chokepoint for *injection* --
  lifecycle state + approval gate. It has no concept of "this secret may have already leaked."
  Whether/how leak status should ever feed back into `check_injectable` is a real open question
  (§ design-discussion.md), not something to assume.
- **`RotationBinding`** (`rotation.py`, `PORTUNUS_HOME/rotation-bindings.json`) records, per
  *provider*, whether Portunus has a real or stub rotation adapter and an account/context hint.
  It does NOT track "this specific reference needs rotating right now" -- that's a new,
  per-reference concern this epic introduces, distinct from and layered on top of
  `RotationBinding`.
- **Locked-JSON-store precedent** (`views.py`, `roles.py`, `rotation.py`) -- every new small
  store this project has added recently follows the same shape: a dataclass, a
  `PORTUNUS_HOME/<name>.json` file, 0600, atomic replace, every mutator wrapped in one
  `flock_path()` acquisition from the start (`filelock.py`). This epic's leak-status store
  follows the same shape, not a new pattern.
- **`crawl.py`'s "bundle context, never call an LLM, never write a value" posture** is the
  closest precedent for "a tool that reads broadly across the vault/filesystem and reports."
  Its AST-level "never calls `.access(` on a returned value" structural test is the template
  this epic's own value-never-leaves-scan-scope guarantee should follow -- adapted, since THIS
  module's whole job is calling `.access()` (to get the value to search FOR), which crawl.py's
  never does. The invariant here is narrower and stricter: `.access()` is called, but the
  returned value must never be returned, printed, logged, or written anywhere except as input
  to an in-memory substring search.

## 3. Real scale data (checked live, read-only, this machine)

Confirmed the kind of surface this needs to scan is not small, and naive "read every byte of
every candidate file, every run" does not hold up:

- `~/.claude/` — **3.4 GB**, **4,421** `.jsonl` conversation transcript files. Several single
  transcripts exceed 100 MB (the largest observed: 506 MB).
- `~/.zsh_history` — ~70k lines, actively appended to.
- `~/Library/Logs/` — **1.1 GB** across many application logs.
- The real vault has **393 references** (per portunus-metadata-crawl's own planning check) --
  meaning a naive per-run "search every secret value against every byte of every file" is
  O(393 × several GB) *every invocation*, which does not scale to "run this regularly."

This directly shapes the design: v1 needs (a) incremental scanning (track a per-file
byte-offset watermark so a re-scan only reads bytes appended since the last run — transcripts
and logs are append-only in practice; a shrunk/rotated file resets its watermark), and (b) an
explicit, configured (not blindly-defaulted) set of scan paths, since silently walking a user's
entire `~/.claude` conversation history the first time this feature runs is both a performance
problem and a "did I actually consent to this" problem.

## 4. Why this needs the heavier H/V process, not another research-brief+design-discussion pass

Four genuinely interdependent, non-trivial design decisions, each with real failure modes if
gotten wrong, none of which is a mechanical implementation choice:

1. **How matching works without the match mechanism itself becoming a leak vector** -- the
   scanner necessarily holds decrypted values in memory to search for them; every line of this
   feature has to hold to the secret-boundary-invariant at least as strictly as `resolve`/
   `inject` already do, in a NEW code path that's easy to get subtly wrong (e.g. an exception
   traceback that includes the value, a debug log line, a truncated-but-still-partial "preview"
   of the match).
2. **Scale/performance** -- confirmed above with real numbers; the naive approach doesn't
   survive contact with the user's actual `~/.claude` directory.
3. **Escalation semantics** -- "slowly escalates" implies a real state machine (severity over
   time and/or over re-detection), a new persisted store, and a policy question (does escalation
   ever become enforcement, e.g. blocking `check_injectable`, or does it stay advisory like
   `roles.json` stayed advisory this epic's predecessor shipped?).
4. **Default scope/safety** -- what gets scanned by default, and whether "by default" should
   mean "nothing, until configured" given the privacy and performance stakes of the user's own
   real conversation history and shell history.

This is the same shape of justification portunus-vault-trust-and-access used for its own H/V
escalation (`docs/design-discussion.md` in that epic, §0). The horizontal plan below inventories
what a full leak-detection-and-response system COULD look like; the vertical plan sequences a
safe, real, independently-shippable v1 out of it.
