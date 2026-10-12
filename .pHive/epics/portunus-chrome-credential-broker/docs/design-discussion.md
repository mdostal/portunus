# Design discussion: Chrome saved-login broker

## 1. Shape

A new backend kind, `chrome`, duck-typed into `_make_backend_router` exactly like `OAuthBackend`
(`portunus-oauth-token-broker`). Three new pieces:

- **`chrome_discovery.py`** — pure discovery, zero decryption. Finds Chrome's `User Data` root
  per platform, reads `Local State` for profile dir → display-name mapping, and for each profile
  copies `Login Data` to a temp path and lists `(origin_url, username_value)` pairs. No
  `password_value` touches this module at all — it is architecturally incapable of returning a
  secret, which is the cheapest way to guarantee the metadata-listing path never leaks one.
- **`chrome_crypto.py`** — one function per platform: `macos_key() -> bytes`,
  `linux_key(profile_dir) -> bytes | None` (`None` = no keyring, Chrome stored it unprotected —
  the caller must surface this explicitly, not silently decrypt-as-if-encrypted). Both funnel
  into one `decrypt(blob: bytes, key: bytes | None) -> str` that handles the `v10`/`v11` prefix
  dispatch (Chromium's own versioning of its encrypted blob format) and the no-key plaintext
  case.
- **`ChromeBackend`** (`backend.py`) — `sm_name` scheme `"chrome:<profile>:<origin>:<username>"`.
  `.access()` locates the row via `chrome_discovery`, decrypts via `chrome_crypto`, returns the
  password. No caching of the decrypted value beyond the single call (unlike `OAuthBackend`'s
  access-token cache — there's no "mint a new one," re-decrypting is cheap and avoids holding a
  plaintext password in memory any longer than necessary).

CLI surface, mirroring `portunus oauth`:
- `portunus browser profiles` — list discovered Chrome profiles (dir name + display name +
  which platform-specific store protects each one, including the "unprotected!" case on Linux).
  Metadata only.
- `portunus browser list --profile <name>` — list `(origin, username)` pairs for that profile.
  Metadata only, never a password.
- Resolution happens through the **existing** `portunus resolve` / `resolve_exec` /
  `resolve_to_tempfile` paths once a reference is registered against `sm_name:
  chrome:<profile>:<origin>:<username>` — no new injection mechanism, reusing the one boundary
  Portunus already guarantees never exposes a value to an LLM/agent context.

## 2. Profiles, concretely

"Profiles" in the user's ask maps directly onto Chrome's own profile concept (`Profile 1`,
`Default`, etc. under `User Data`) — not a Portunus-invented abstraction. `portunus browser
profiles` surfaces exactly what Chrome itself calls them (via `Local State`'s
`profile.info_cache` display names), so a user picking a profile for a test scenario is picking
the same profile they'd see in Chrome's own profile switcher.

## 3. Security invariants (non-negotiable, same bar as every other Portunus backend)

- A decrypted Chrome password never touches a log line, print, exception message, or audit
  entry — identical invariant to every existing backend, re-verified here because this is the
  first backend where the plaintext secret pre-dates Portunus's own involvement (every other
  backend either mints or stores the value itself; here Chrome already has it, which makes it
  *more* tempting to "just log the origin + whether a password was found" during development —
  the origin is fine to log, the password is never fine to log, and development-time debug prints
  are exactly how PANT's own oauth live-proof incident happened this session; see
  `.pHive/epics/portunus-oauth-token-broker/docs/lessons-learned.md`).
- `Login Data` is **copied**, never opened in place — Chrome may hold it locked while running,
  and even if it doesn't, Portunus must never risk a write-mode open against a file the real
  browser owns.
- The macOS Keychain read is a **read-only** `security find-generic-password` style call — never
  a write, never a different service/account than Chrome's own `"Chrome Safe Storage"/"Chrome"`.
- Linux's no-keyring case (plaintext `v10`) is surfaced as a warning in `portunus browser
  profiles` output, not silently treated as a normal decrypt path — the user should know Chrome
  itself isn't protecting that profile's passwords.

## 4. Testing strategy

Can't assume CI (or even the developer's own machine, reliably) has a real Chrome profile with
known fixture logins. Two-tier:
- **Unit**: synthetic `Login Data` SQLite fixtures (hand-built, known `v10`/`v11` blobs encrypted
  under a known test key) + a stubbed keychain/keyring lookup — exercises `chrome_crypto.py` and
  `chrome_discovery.py` without touching any real OS secret store.
- **Live smoke test** (same discipline as every other epic this session): run against this
  machine's actual local Chrome install if one with saved logins exists; if not, document that
  the live proof was skipped and why, rather than fabricating one. Never print the decrypted
  value during the live proof — compare a SHA-256 digest against an independently-obtained
  expectation, same fix applied after the OAuth broker's live-proof incident.

## 5. Self-grill

**Q: Does reading another app's (Chrome's) on-disk secret store cross a line Portunus hasn't
crossed before?**
A: Yes, architecturally — every prior backend either stores a value Portunus put there itself
(`LocalEncryptedBackend`, `OAuthBackend`'s bundle storage) or calls an API that mints a credential
on request (`GcloudBackend`, `OAuthBackend`'s access-token mint). This is the first backend that
reads a *pre-existing* secret another application manages, using that application's own
same-user OS-level access pattern. That's exactly why platform scope and the "never log the
value" invariant are called out explicitly rather than assumed to carry over for free — see §3.
Resolved: acceptable because it is same-user, same-machine, no privilege escalation, and
functionally identical to what already-public tools like `browser_cookie3` do for the same
legitimate "read your own saved browser credentials for test automation" use case the user
described. Flagged, not blocking.

**Q: Is "profiles" scope actually "multiple Chrome profiles" or could the user have meant
something else (e.g. Portunus's own `project`/`env` scoping)?**
A: Re-read against the user's literal words — "so we have profiles and can easily run through
test scenarios" — in context of "connect and use it as another secret broker... inject, pull,
change... Chrome browser logins." This reads as Chrome's own profile concept (switch which saved
logins a test run pulls from), not Portunus's existing `org/project/env` scope fields. Resolved
by §2: map 1:1 onto Chrome's own profiles, don't invent a parallel scoping concept.

**Q: Does deferring Windows leave the epic half-finished in a way that undersells the ask?**
A: User explicitly chose "defer Windows entirely" after seeing the CVE-grade alternative. Not a
unilateral descope — a direct answer to a direct question. Tracked as a named follow-up in
research-brief.md §1, not silently dropped.

**Q: Is "copy the Login Data file" itself a TOCTOU risk (file changes between copy and read)?**
A: No worse than any other point-in-time credential read — Portunus reads a snapshot, decrypts
it, returns one value. If Chrome rewrites `Login Data` a moment later, the next resolve just
reads the new snapshot. No concurrent-write hazard Portunus introduces, since Portunus never
writes to the file.

**Q: No findings left open from this pass** — unresolved_count: 0. Proceeding to stories.
