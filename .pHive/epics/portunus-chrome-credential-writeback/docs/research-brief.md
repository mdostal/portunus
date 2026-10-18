# Research brief: Chrome saved-login write-back / rotate

Follow-up to `portunus-chrome-credential-broker` (shipped v0.34.0, read-only). Tracked as
GitHub #182 / PANT-1144.

## Requirement (verbatim)

> Start with 1 read through only, but ideally we should be able to inject and whatnot long term,
> put it in a backlog vision to revisit the update later and that can probably be done through
> APIs or something to roll/change the keys instead.

The constraint from the issue: **no raw SQLite mutation of Chrome's `Login Data`**. Any write has
to go through a path Chrome itself owns.

Each claim below is labeled. **Verified** means checked against a primary source on 2026-10-18,
with the link given. **Unverified** means it is still a hypothesis and Story 01 (the live spike)
has to settle it before any implementation story starts.

## 1. Candidate write paths

### 1a. Raw `Login Data` write: rejected (issue's own position, reinforced)

- Chrome may hold the file open. A write next to a live browser can corrupt it or race with
  Chrome's own writes (issue body).
- Even with Chrome closed, a raw write skips Chrome's own bookkeeping: password-sync metadata,
  the separate account-store database (`Login Data For Account`, for Google-account-synced
  passwords), and `insecure_credentials`/notes side tables. A synced profile would reconcile
  against the server on next launch and could revert or duplicate the row. *Unverified in
  detail. Listed as a reason, not load-bearing, since 1a is rejected anyway.*
- Not revisited by this epic.

### 1b. Third-party extension calling Chrome's password API: **not possible on stable Chrome**

- **Verified.** The only extension API that edits saved passwords is `chrome.passwordsPrivate`.
  Its permission entry in Chromium's `_permission_features.json` is `"channel": "trunk"`,
  `"location": "component"`. Only Chrome's own built-in component extensions can hold it, and
  only on trunk builds. Its API entry in `_api_features.json` also exposes it to the
  `chrome://password-manager/*` WebUI. A Portunus-shipped extension can never request it.
  - https://chromium.googlesource.com/chromium/src/+/main/chrome/common/extensions/api/_permission_features.json
  - https://chromium.googlesource.com/chromium/src/+/main/chrome/common/extensions/api/_api_features.json
- **What this means for the issue's first idea.** "A Chrome extension or native-messaging host
  Portunus controls, that Chrome itself uses to update its own stored credential" cannot work as
  a *direct password API call*. A native-messaging host is only a stdin/stdout pipe between a
  native process and an extension, and adds no capability the extension lacks.
  (https://blog.chromium.org/2013/10/connecting-chrome-apps-and-extensions.html)
  The extension + native-host *transport* is still useful, as the delivery mechanism for 1c.

### 1c. Credential Management API (`navigator.credentials.store`) on the target origin: **leading candidate**

- **Verified.** Chrome supports `PasswordCredential` (since Chrome 51). `navigator.credentials.store()`
  hands a credential to Chrome's password manager, and Chrome asks the user whether to store it.
  This is Chrome's own write path, used by Chrome's own UI, with the user's explicit confirmation.
  (https://developer.chrome.com/blog/credential-management-api ,
  https://web.dev/articles/security-credential-management-save-forms)
- **Unverified (Story 01 must answer):**
  1. When a row for the same (origin, username) already exists, does Chrome show an **"Update
     password?"** prompt and overwrite in place? Or does it create a duplicate, or do nothing?
  2. Can the call come from an extension **content script (isolated world)**? Or must it run in
     the page's **main world**? In the main world, the page's own (possibly third-party) scripts
     could monkey-patch `navigator.credentials.store` and capture the value. That is a
     secret-boundary concern (design-discussion §3).
  3. Does it require a user activation (gesture) in the tab?
  4. Which store does the update land in (profile store vs. account store) when both have the row?
- This path needs a tab open on the real origin. That is a feature, not a bug: Chrome binds the
  credential to the origin it is stored from, so Portunus cannot write a password for
  `bank.example` from some other page.

### 1d. CDP automation of the user's real Chrome: **blocked by Chrome itself**

- **Verified.** Since Chrome 136, `--remote-debugging-port`/`--remote-debugging-pipe` are ignored
  when Chrome runs on its *default* user-data directory. The stated reason is attackers using
  remote debugging to extract cookies. Google's guidance is a custom `--user-data-dir`, or
  Chrome for Testing.
  (https://developer.chrome.com/blog/remote-debugging-port)
- So CDP/Playwright **cannot** drive the operator's everyday profiles. It **can** drive a
  Portunus-owned profile under a custom `--user-data-dir` (see §2 and Q1 in the design doc).
- Even under CDP, Chrome's "Save/Update password?" bubble is browser UI, not page DOM. CDP page
  automation does not click it. *Unverified whether any CDP domain exposes it. Story 01 checks
  the current protocol, since the docs site 404'd during this research.*

### 1e. Chrome's CSV import (`chrome://password-manager/settings` → Import): **fallback only**

- **Verified (weakly):** the import button lives at `chrome://password-manager/settings` and opens
  the OS file picker (community/moderator reports, no official spec found).
- **Unverified:** current conflict behavior when an imported row matches an existing
  (origin, username). Reports conflict: a 2016 test says it overwrote, while a Vivaldi report
  says it did not. Story 01 tests it on current stable.
- Inherent cost: the new password must sit in a **plaintext CSV on disk** until the user imports
  it. That is a boundary-sink exception Portunus has never made. Only acceptable, if at all, as a
  0600 tempfile in a private dir with a hard TTL and guaranteed unlink. The design keeps it as a
  documented fallback, not a default.

### 1f. Chrome's own automated password change: **not usable as an API**

- **Verified (press reports, 2025-2026).** Chrome's "change it for me" automatic password change
  exists only for a small partner set of sites. A broader Gemini-driven version is an unfinished
  Canary experiment behind a flag (Android Authority, Aug 2026). No API for third parties.
  Not a dependency for this epic.

## 2. Two different targets hide inside "rotate"

1. **The site's password.** Actually changing the credential at `example.com`. That is a
   *provider rotation*, the same shape as Portunus's existing `rotation.py` adapters
   (`RotationAdapter.rotate()` → verify → store → optional retire-old). For arbitrary websites
   there is no API. `/.well-known/change-password` only *locates* the change form. It does not
   perform the change.
2. **Chrome's stored copy.** After the site's password changes, by Portunus or by a human, make
   Chrome's saved login match. **This is the part #182 actually asks about** ("roll/change the
   keys", "Chrome's own write path, not ours").

The design (design-discussion.md) scopes the epic to **(2), Chrome's stored copy, as a rotation
sink**. Any rotation adapter, or a human, produces a new value. Portunus delivers it into Chrome
through Chrome's own write path. Site-side change for arbitrary websites is out of scope, as its
own unbounded per-site automation problem.

## 3. Test-scenario framing (the original use case)

The parent epic's ask was test automation ("so we have profiles and can easily run through test
scenarios"). For that use case, a **Portunus-owned Chrome profile** (custom `--user-data-dir`)
may beat writing into the operator's real profiles. CDP works there (§1d). Nothing Portunus does
can corrupt the operator's daily browser. And the profile can be thrown away. Whether the
operator wants "write into my real Chrome" or "Portunus-managed test profiles", or both, is the
decision that most changes the build (design-discussion Q1).

## 4. inconsistency_risk_signals (for grill)

- The issue's option 1 ("extension Chrome uses to update its own credential") assumes an extension
  password-write API that **does not exist** on stable (§1b, verified). The design keeps the
  extension, but as a *carrier* for 1c.
- The issue's option 2 ("drive Chrome's password-change UI via automation") hits the Chrome 136
  remote-debugging lock on default profiles (§1d, verified).
- 1c's main-world vs. isolated-world question decides whether the secret is exposed to page
  scripts. The leading design cannot be finalized until Story 01 answers it.
- Account-store (`Login Data For Account`) rows: the read path's `chrome_discovery.list_logins()`
  only reads `Login Data`. A write-back that verifies by re-reading would miss account-store rows.
  Story 02 must at least detect them.
