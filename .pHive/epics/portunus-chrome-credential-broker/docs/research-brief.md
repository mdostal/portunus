# Research brief: Chrome saved-login broker

## Requirement (verbatim)

> We need to be able to integrate the chrome browser logins as well and nail that across, make an
> integration to allow us to connect and use it as another secret broker when we are doing testing
> and stuff so we can inject, pull, change, etc so we have profiles and can easily run through test
> scenarios.

Scoped down after a clarifying round with the user (2026-10-12):
- **Platforms: macOS + Linux only, this epic.** Windows deferred — see §3.
- **Scope: read-through only, this epic.** "Inject" means exposing a Chrome-saved login through
  Portunus's *existing* boundary-only resolver (the same `{{secret:NAME}}` / `resolve_exec` /
  `resolve_to_tempfile` paths every other backend already uses), not mutating Chrome's own
  `Login Data` file. "Change"/rotate-into-Chrome is an explicit backlog item — see §5.

## 1. How Chrome actually stores saved logins (verified, not assumed)

Every platform shares the same outer shape: each Chrome **profile** directory contains a
`Login Data` SQLite database (table `logins`, with `origin_url`, `username_value`, and a
`password_value` BLOB per row). The schema and most columns are plaintext SQLite — only the
credential-bearing fields are encrypted, and the encryption key itself is *not* in the SQLite
file. It lives in an OS-level secret store, platform-specific:

**macOS** — confirmed via Talos's 2026 vulnerability report (TALOS-2026-2444, Chrome
148.0.7778.216, Apple Silicon):
- Chrome keeps a random 16-byte value in the user's own **login keychain**, service name
  `Chrome Safe Storage`, account `Chrome`.
- That value is run through **PBKDF2-HMAC-SHA1**, salt `"saltysalt"`, **1003 iterations**, to
  derive a 128-bit **AES-CBC** key.
- That key decrypts the `password_value` blobs in `Login Data` (and the equivalent fields in
  `Web Data` / cookies — out of scope here, logins only).
- A same-user, unprivileged process can read this keychain item the same way Chrome itself does
  (`security find-generic-password -w -s "Chrome Safe Storage" -a "Chrome"` is the canonical
  CLI-level example of the same access Chrome's own process makes). No privilege escalation, no
  exploit — this is the normal trust boundary macOS already grants any process running as the
  logged-in user, same tier as Portunus's own `LocalEncryptedBackend` reading its own vault file.
- Source: https://talosintelligence.com/vulnerability_reports/TALOS-2026-2444

**Linux** — confirmed via Chromium's own docs
(https://chromium.googlesource.com/chromium/src/+/main/docs/linux/password_storage.md) plus a
third-party hands-on writeup:
- Chromium auto-selects a backend by desktop environment: **GNOME Libsecret** (Secret
  Service / gnome-keyring), **KWallet** (4/5/6), or a **plaintext fallback** when neither is
  available.
- When a real keyring/wallet is present, the AES key is stored there (same wrap-then-decrypt
  shape as macOS) and `Login Data`'s encrypted columns are protected by it.
- **When no keyring is available, Chrome does not encrypt the saved passwords at all** — the
  writeup observed a `v10`-prefixed, effectively-plaintext entry in that case. This is a real,
  user-hostile Chrome behavior, not a Portunus design choice; the broker should surface it (warn
  loudly that "this profile's passwords aren't actually protected by anything"), not quietly rely
  on it as a free lunch.
- Override flag `--password-store=gnome-libsecret|kwallet|basic` exists on Chrome's side but is
  not something Portunus controls (it's the *browser's* launch flag, not ours) — the broker must
  detect whichever backend Chrome actually used for a given profile, not assume one.
- Source: https://chromium.googlesource.com/chromium/src/+/main/docs/linux/password_storage.md

**Windows — deferred, not implemented this epic.** Chrome's current Windows protection is "App-Bound
Encryption" (wraps DPAPI with an additional layer tied to a SYSTEM-privileged elevation-service COM
server). The only confirmed way past it right now is **CVE-2025-34091** ("C4 Bomb", CyberArk), a
padding-oracle attack against that elevation service — a live, actively-discussed exploit
technique, not a sanctioned same-user read path. Google has shipped only a partial,
disabled-by-default mitigation as of mid-2025; a full fix is still pending. Building this into
Portunus would mean shipping exploit code against Chrome's own privilege boundary — categorically
different risk tier from the macOS/Linux same-user reads above, and explicitly out of scope per
the user's own call. Filed as a follow-up: track a non-exploit path (e.g. whatever legitimate API
Chrome's own "Export passwords" feature uses) before revisiting.
Sources: https://www.cyberark.com/resources/threat-research-blog/c4-bomb-blowing-up-chromes-appbound-cookie-encryption ,
https://nvd.nist.gov/vuln/detail/CVE-2025-34091

## 2. Profile layout (needs live confirmation during Story 01, not assumed here)

Expected (standard Chrome convention, to be confirmed against a real local install, not asserted
as fact pre-implementation):
- macOS: `~/Library/Application Support/Google/Chrome/<Profile Directory>/Login Data`
- Linux: `~/.config/google-chrome/<Profile Directory>/Login Data`
- A `Local State` JSON file at the `User Data` root lists profile directory names → display
  names (`profile.info_cache`), used to resolve human-readable profile names for CLI output.
- Chrome locks `Login Data` while running (standard SQLite file locking) — Story 01 must confirm
  this live and design around it (copy-before-read, same discipline as any live-DB snapshot read).

## 3. Why this fits Portunus's existing shape

This is architecturally closest to `portunus-oauth-token-broker`: a new `SecretBackend`-shaped
adapter, duck-typed into `_make_backend_router` the same way `OAuthBackend` was, with its own
`sm_name` scheme encoding profile + origin (mirroring OAuth's `"<provider>:<account>"`). Unlike
every backend so far, the *secret material already exists* on disk (Chrome put it there) — there
is no "store" step, only "discover" (list profiles/origins, metadata only) and "resolve" (decrypt
one specific value, boundary-only, same invariant as everything else: never let the decrypted
value touch a log line, print, or audit entry).

## 4. inconsistency_risk_signals (for grill)

- Linux's keyring-selection is Chrome's own runtime choice, not configurable by Portunus — a
  design that assumes "the" Linux decrypt path is wrong; it must branch per-profile.
- The macOS Keychain item is scoped to the **logged-in user**, not per-Chrome-profile — one key
  decrypts every profile's `Login Data` on that machine. Multi-profile support is about which
  *database* to read, not which *key* to fetch.
- "Copy the file before reading" must be verified against Chrome actually holding an exclusive
  lock while running — if it doesn't, the design should still copy defensively (never read a
  live, externally-mutable file in place) but shouldn't invent a blocking problem that isn't real.

## 5. Explicitly future (not this epic)

- Writing/rotating credentials back into Chrome's own `Login Data` ("change"). The user's own
  framing: start read-only, but the long-term vision includes write/rotate, "probably through
  APIs or something to roll/change the keys instead" rather than raw SQLite mutation — i.e.
  investigate Chrome's sync/management-side APIs (enterprise policy, `chrome://password-manager`
  automation, or a future Chrome-exposed API) rather than hand-writing encrypted SQLite rows next
  to a running browser. Needs its own research pass; not decomposed into stories here.
- Windows support (§1).
