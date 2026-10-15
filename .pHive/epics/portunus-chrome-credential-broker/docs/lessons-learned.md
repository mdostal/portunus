# Lessons learned — portunus-chrome-credential-broker

## What shipped

5 stories, all merged to `dev`, built directly in-session rather than through the Pantheon/Auriga
pipeline (see "Pantheon handoff, then takeback" below):

- **01-profile-discovery** (PR #177) — `chrome_discovery.py`: `discover_profiles()`, `list_logins()`.
- **02-macos-decrypt** (PR #178) — `chrome_crypto.py`: `macos_key()`, `decrypt_blob()`.
- **03-linux-decrypt** (PR #179) — `chrome_crypto.py`: `linux_key()`, `key=None` plaintext fallback.
- **04-backend-router-wiring** (PR #180) — `ChromeBackend`, `_make_backend_router` wiring, `portunus
  browser profiles`/`list` CLI.
- **05-closeout** (this PR) — docs, version bump, follow-up issues.

## Live proof: what was actually verified, and what wasn't

**macOS (Story 02): fully live-verified, far beyond the minimum.** Decrypted all 1020 real saved
logins across every profile on the implementing machine (`Default` profile alone had 1084 rows),
zero failures. This resolved a real, disclosed risk: the research brief couldn't confirm Chrome's
blob format byte-for-byte from public sources alone (PBKDF2-HMAC-SHA1/`saltysalt`/1003
iterations/AES-128-CBC/static 16-space IV/`v10` prefix) — it now has independent, live
confirmation, not just one secondary source.

**End-to-end resolve path (Story 04): live-verified.** Registered a real `chrome:`-backed
reference and resolved it through the actual `_make_backend_router` dispatch (not a mock), then
compared a SHA-256 digest of the result against an independently-computed expectation (hashed
from a separate script that read the real row directly). Digests matched exactly. The raw
decrypted password never appeared in any script output, test assertion message, or terminal
history — every comparison in this epic's own verification scripts was hash-only, by design, the
same discipline the OAuth broker adopted only after a real incident.

**Linux (Story 03): NOT live-verified.** The implementing machine is macOS. `linux_key()`'s Secret
Service and KWallet paths are fixture/mock-only — tests mock `secret-tool`/`kwallet-query`
subprocess calls rather than exercising a real GNOME keyring or KWallet daemon. This is a real,
disclosed platform-coverage gap, documented here and in `docs/backends.md`, not glossed over as
"tested."

**`classify_store()`'s Linux labels (Story 04) are a heuristic, not a guarantee.** It checks for a
running `gnome-keyring-daemon`/`kwalletd*` process (the same desktop-environment signal Chromium
itself uses to pick a backend) rather than probing the actual stored key — deliberately, so the
metadata-only `portunus browser profiles` command never has to touch `chrome_crypto` at all. The
label it prints could in principle be wrong if, e.g., a keyring daemon is running but holds no
Chrome entry; `ChromeBackend.access()` is the real, authoritative path and will surface its own
clear error if the label was optimistic.

## Corrections to the epic's own planning

- **`_make_backend_router` dispatches by `ref.backend`, not by parsing `sm_name` text.** Story 04's
  acceptance criterion described OAuth's existing dispatch as "sm_name prefix" dispatch. Reading
  the actual code (`cli.py`'s `_for_kind`/`router`) showed this is wrong: both `OAuthBackend` and
  now `ChromeBackend` are selected by the `Reference.backend` field (set via `portunus drop
  --backend oauth|chrome`), which is a different mechanism than inspecting `sm_name`'s text.
  Implemented against the real mechanism; noted here so the next epic that reads this one's
  planning docs doesn't inherit the same inaccurate description.
- **`sm_name` parsing needs more care than a 4-way colon split.** `chrome:<profile>:<origin>:<username>`
  looks like a clean 3-colon split, but origin URLs contain their own colons (`https://`, an
  optional port). `ChromeBackend.access()` splits the profile off the front (profile names never
  contain `:`) and the username off the back (usernames don't either), treating everything in
  between as the origin — handles both ported and unported origins correctly. A naive
  `sm_name.split(":", 3)` breaks on exactly this.
- **`sqlite3.Cursor.execute` and `sqlite3.Connection.execute` are immutable in this Python
  version (3.13)** — `monkeypatch.setattr(sqlite3.Cursor, "execute", ...)` raises `TypeError:
  cannot set 'execute' attribute of immutable type`. The structural "never selects password_value"
  test (Story 01) was rewritten to monkeypatch `sqlite3.connect` itself and use the real
  `set_trace_callback()` API (built for exactly this purpose) instead of patching the C-level
  class method. Worth remembering for any future structural SQL-text assertion in this codebase.

## Pantheon handoff, then takeback

This epic's planning (docs + 5 story YAMLs, PR #176) was handed off to the Pantheon/Auriga
multi-agent pipeline on 2026-10-13, with this session committed to a post-merge audit role rather
than building it directly. As of 2026-10-14, the Pantheon build agents had filed no story PRs and
were reportedly repeatedly classifying the Keychain/Secret-Service decryption work as a
cybersecurity threat rather than the scoped, read-only, same-user credential read it actually is —
confirmed authorized security-research/testing context, not exploit development. Mathew's call:
take the epic back and build it directly in this session, which is what produced PRs #177-#180
and this closeout. The planning docs and story YAMLs (written before the handoff) needed no
rework — they transferred cleanly to direct execution.

## Follow-ups filed (not evaporated into a doc nobody re-reads)

- [#181](https://github.com/mdostal/portunus/issues/181) — Windows support (blocked on a
  non-exploit read path existing; CVE-2025-34091 is the only confirmed bypass today, and that's
  out of scope).
- [#182](https://github.com/mdostal/portunus/issues/182) — write-back/rotate, Mathew's explicit
  long-term ask, deliberately not attempted in this epic.
