# Spike results: Chrome write paths (Story 01, PANT-1422)

Run date: 2026-11-20. Spike only: no production code changed. Harness and raw results are in
`../spike/` (see `../spike/README.md`).

## Environment (applies to every finding unless a finding says otherwise)

| | |
|---|---|
| Binary | **Google Chrome for Testing 153.0.8010.12** (Playwright `chromium-1243`, `chrome-linux64/chrome`). **Not** Google Chrome stable. |
| Platform | Linux x86_64 (kernel 7.2.11-arch1-1), Debian 12 container |
| Display | headed on Xvfb `:99` (1280x900, no window manager). F1 and F3 were also repeated `--headless=new`. |
| Profile | fresh throwaway `--user-data-dir` per run (`~/spike-prof-*`), `--password-store=basic` (no keyring present), **no Google account signed in** |
| Origin | `http://localhost:8765` served by `python3 -m http.server` (pages in `../spike/site/`) |
| Credentials | dummy: username `spike-user`, passwords `dummy-old-<random>` / `dummy-new-<random>`, regenerated per run. Only 16-hex-char SHA-256 prefixes were recorded. |
| Seeding | the "existing row" was created via `chrome.passwordsPrivate.addPassword` on `chrome://password-manager` (Chrome's own write path, see F6). No raw `Login Data` write anywhere. |
| Read-back | a copy of `Login Data` / `Login Data For Account`, opened read-only. Row count, `origin_url`, `date_password_modified`, and the SHA-256 digest of the decrypted value. **ChromeBackend.access() was also run on every row and fails on Linux** (F7), so the digest comparison used a harness-local decrypt with Chromium's Linux basic-store key. |

Not exercised at all: **Google Chrome stable, macOS, a signed-in profile (account store), a
real keyring (Secret Service/KWallet)**. Each finding repeats which of these it is missing.

## Summary

| # | Question (research-brief) | Answer on CfT 153 / Linux | Status |
|---|---|---|---|
| F1 | §1c.1 `credentials.store` on an existing (origin, username) | **Updates the row in place, with no consent click.** Chrome logs `Decision: UPDATE the saved credential with a new password on Profile Store` and shows an after-the-fact **"Password updated"** notice, not an "Update password?" prompt. One row, new digest. | verified (CfT/Linux); stable + macOS not verified |
| F1b | (new) `credentials.store` for a username not yet saved | "Save password?" bubble (`PENDING_PASSWORD_STATE`). **No row is written until a human clicks Save.** `store()` still resolves successfully. | verified (CfT/Linux) |
| F2 | §1c.2 ISOLATED vs MAIN world | **ISOLATED world works**, and page-script patches are invisible to it (hostile page captured nothing). Plain MAIN world **leaks**: the hostile page's patched `store` captured the username and a password digest equal to the delivered value. MAIN with natives captured at `document_start` dodged this test page's patches, but is fragile. | verified (CfT/Linux) |
| F3 | §1c.3 user activation | **Not required.** It works with `navigator.userActivation.isActive === false` from extension `scripting.executeScript` (both worlds) and from CDP `Runtime.evaluate {userGesture:false}`, headed and headless. | verified (CfT/Linux) |
| F4 | §1c.4 local vs account store | Only the profile store could be exercised. The update landed on the **Profile Store** (`Login Data`); `Login Data For Account` stayed empty. Account-store behaviour **not verified**: no signed-in Google account, and `addPassword({useAccountStore:true})` is refused. | partially verified; account store carried forward |
| F5 | §1e CSV import conflict | **Does not overwrite.** `importPasswords` returns `CONFLICTS`, imports 0 and leaves the existing row untouched. A follow-up `continueImport([id])` (the "replace" choice) returns `SUCCESS` and replaces the row in place. The file picker is a native GTK dialog that CDP cannot drive. | verified (CfT/Linux, via the WebUI's API); Chrome stable's Settings button not clicked |
| F6 | §1d CDP bubble | **No CDP domain for passwords** in protocol 1.3 of 153. `Input.dispatchKeyEvent` cannot reach the bubble. But (a) updates need no bubble (F1), and (b) on a Portunus-owned profile CDP can run `chrome.passwordsPrivate.addPassword` / `changeCredential` in the `chrome://password-manager` WebUI, which writes through Chrome's own store with no prompt, headless. | verified (CfT/Linux) |
| F7 | (new, read path) | **`ChromeBackend.access()` cannot decrypt any Linux row.** Linux Chromium derives its key with **1** PBKDF2 iteration, but `chrome_crypto` uses 1003. Its no-keyring path also treats the AES-encrypted v10 payload as plaintext. This blocks digest verify (Story 05) on Linux. | verified (CfT/Linux, basic store); keyring path inferred, not live-tested |

**Gating questions, stated plainly:**
- **Update-existing-row:** answered for Chrome for Testing 153 on Linux. It updates in place
  with no prompt.
- **JS world:** answered for Chrome for Testing 153 on Linux. Use ISOLATED; MAIN is unsafe.
- **Still not verified:** both answers on **Google Chrome stable** and on **macOS**, and on a
  signed-in, account-store profile. Story 04 must re-check F1 and F2 on the operator's real
  platform before driver A ships.

## Findings in detail

### F1: `credentials.store` updates an existing row in place, with no prompt

Setup: seed `(http://localhost:8765, spike-user, OLD)`, open the origin, call
`navigator.credentials.store(new PasswordCredential({id:'spike-user', password:NEW}))`.

Every run, 10 variants in total, produced the same three internals lines
(`chrome://password-manager-internals`, open in a side tab):

```
CM API save a credential: origin=http://localhost:8765, CredentialType=CredentialType::CREDENTIAL_TYPE_PASSWORD
Message:  Decision: UPDATE the saved credential with a new password on Profile Store
The new state of the UI:  UPDATE_CONFIRMATION_STATE (3)
```

The bubble reads **"Password updated"**. It is a notice after the fact, with no Update/Cancel
choice (`../spike/results/click-update-bubble.png`). Read-back: exactly one row,
`date_password_modified` bumped, digest equal to NEW.

| Variant | activation | rows after | digest |
|---|---|---|---|
| page button click (`click`) | yes | 1 | = NEW (`43b5929f…`); raw JSON was printed, not saved to `results/`, only the screenshot `click-update-bubble.png` was kept |
| Playwright `page.evaluate` (`page-nogesture`, mislabeled: Playwright grants activation) | yes | 1 | = NEW |
| CDP `Runtime.evaluate userGesture:true` | yes | 1 | = NEW |
| CDP `Runtime.evaluate userGesture:false` | **no** | 1 | = NEW |
| extension `executeScript` ISOLATED | **no** | 1 | = NEW |
| extension `executeScript` MAIN | **no** | 1 | = NEW |
| hostile page + ISOLATED / MAIN / MAIN-native | **no** | 1 | = NEW (all three) |
| seed URL `http://localhost:8765/login/form` (path differs from the calling page) | **no** | 1 | = NEW, row keeps `origin_url …/login/form` |
| headless: CDP no-gesture, extension ISOLATED | **no** | 1 | = NEW (both) |

Raw: `../spike/results/res-*.json` (call outcome + internals), `vr-*.json` (read-back).

**Design consequence (contradicts design-discussion §1/§5 as written).** The design assumed
"Chrome's own confirmation ('Update password?') is kept … a human click on Chrome's own bubble
is the consent step." On this build, Chrome does **not** ask before updating a known
username via the CM API. So any code that can run script on the origin can overwrite a saved
password without a click: Portunus's extension, but equally the site itself. Consent for an
update therefore has to be **Portunus-side** (an explicit CLI confirmation). It cannot be
delegated to Chrome. Also, `store()` resolves successfully in every case, including the F1b
"pending, never saved" case. So the extension **cannot** report `stored` vs `dismissed`, and
the read-back digest is the only truth.

### F1b: new username → "Save password?" (needs a click)

With no seeded row, the same call yields `PENDING_PASSWORD_STATE (1)` and a "Save password?"
bubble with Never / No thanks / Save (`../spike/results/newrow-save-bubble.png`). No row is
written (`vr-newrow-*.json`: `[]`). `store()` resolved `ok:true`.

### F2: JS world

Test page `../spike/site/hostile.html`: its first `<head>` script patches
`navigator.credentials.store` (instance), `CredentialsContainer.prototype.store`, and the
`PasswordCredential` constructor. It records `id` plus a SHA-256 prefix of any password it
sees.

| Injection | stored? | page captured |
|---|---|---|
| ISOLATED (`scripting.executeScript world:'ISOLATED'`) | yes | **nothing** (`[]`) |
| MAIN (`world:'MAIN'`), calling `navigator.credentials.store` | yes | **`ctor` + `patched-store` with `pwSha` = delivered value's digest**: leak confirmed |
| MAIN using `CredentialsContainer.prototype.store` and `PasswordCredential` captured by a `document_start` MAIN content script (`../spike/ext/main_capture.js`) | yes | nothing (`[]`) |

Residual risk of the MAIN-native variant (**not tested**): the call still goes through
page-reachable built-ins (`Function.prototype.call`, and the `PasswordCredential` instance
the page can observe through getters), and it depends on winning the
`document_start` race on every navigation. ISOLATED needs none of this, so MAIN is
unnecessary.

### F3: user activation not required

See the F1 table: `navigator.userActivation.isActive` was `false` in the extension and in the
CDP `userGesture:false` runs, and all of them updated. (New-row saves were only tested without
activation. They still showed the bubble.)

### F4: which store

Every update logged `on Profile Store` and changed `Login Data`. `Login Data For Account`
exists but is empty in every profile. The account store **could not be exercised**: there was
no Google sign-in in the container, and `passwordsPrivate.addPassword({useAccountStore:true})`
returned "Could not add the password. Either the url is invalid, the password is empty or an entry
with such origin and username already exists." (`res-acct-ext-isolated.json`). That attempt
ran after a profile-store seed of the same (origin, username), so the message does not
separate "no account store without sign-in" from "duplicate". Either way, no account row
was created (`vr-acct-ext-isolated.json`). **Carried forward**:
Story 02's preflight must still detect account-store rows. The internals `Decision:` line
naming the store, plus the per-store `Number of results from:` counts, show how a live check
could tell which store changed.

### F5: CSV import conflict

Driven through `chrome.passwordsPrivate.importPasswords('DEVICE')` on `chrome://password-manager`.
This is the API behind Settings → Import; **the Settings button itself was not clicked.** Chrome
opens a native GTK file chooser (`../spike/results/csv-import-gtk-dialog.png`). Playwright's
`filechooser` event does not fire, so the path had to be typed with X11 XTEST
(`../spike/xtype.py`). That is OS-level UI automation, used only to observe the behaviour. The
CSV was 0600 in a 0700 dir and unlinked in `finally` (`csv_unlinked: true` on every run).

- Matching row exists → `{status:"CONFLICTS", numberImported:0, displayedEntries:[{status:"VALID", username:"spike-user", url:"http://localhost:8765"}]}`. Existing row untouched (digest = OLD).
- `continueImport([0])` → `{status:"SUCCESS", numberImported:1}`. One row, digest = NEW (replaced in place, no duplicate).

So: no silent overwrite and no duplicate; conflicts need an explicit second "replace" step.
Both reports cited in the research brief are out of date for this build. **Chrome stable not
verified.**

### F6: CDP and the bubble

- Protocol 1.3 of 153 domains (from `/json/protocol`): Accessibility … Autofill … WebAuthn …
  There is **no password-manager domain**. `Autofill` only has `trigger` (card/address),
  `setAddresses`, `enable`, `disable`. `Browser.executeBrowserCommand` only accepts
  `openTabSearch`, `closeTabSearch`, `openGlic`.
- `Input.dispatchKeyEvent` (Enter / Space / Tab / Enter) with a "Save password?" bubble open
  went to the page. The bubble stayed and no row was written (`res-newrow-accept.json`).
- **The new finding:** on a Portunus-owned profile, CDP can open `chrome://password-manager` and
  `Runtime.evaluate` there sees `chrome.passwordsPrivate` (full key list in `../spike/probe1.mjs`
  output: `addPassword`, `changeCredential`, `requestCredentialsDetails`, `importPasswords`,
  `continueImport`, `getSavedPasswordList`, …). Measured, headless, no prompt and no OS reauth on
  Linux:
  - `addPassword({url, username, password, note:'', useAccountStore:false})` creates a row (used
    for every seed in this spike).
  - `getSavedPasswordList()` → `requestCredentialsDetails([id])` → `changeCredential({...cred, password:NEW})`
    updates in place (digest = NEW, one row).
  - This is Chrome's own PasswordStore write path (the one its settings UI uses), not a raw
    `Login Data` write. It needs no test-origin page and no bubble.
  - Caveat: `requestCredentialsDetails` returns the **current plaintext** into the WebUI's JS
    context. A driver must keep it there and never return it over CDP. The harness only returns
    `Object.keys()`. Whether macOS demands OS reauth for it is **not verified**.
  - The research brief's §1b claim still holds: no *extension* can hold `passwordsPrivate`. This is
    the WebUI's own binding, reachable only because CDP is allowed on a custom `--user-data-dir`.

### F7: ChromeBackend read path is broken on Linux (blocks Story 05 verify on Linux)

The same v10 blob from a basic-store profile:
- PBKDF2-SHA1(`peanuts`, `saltysalt`, **1** iteration) → decrypts, digest = seeded value.
- PBKDF2-SHA1(`peanuts`, `saltysalt`, **1003** iterations) → "invalid PKCS7 padding".
- `ChromeBackend.access()` → `BackendError: no-keyring fallback blob is not valid UTF-8`. It
  takes the `key is None` branch and decodes the ciphertext as plaintext.

Linux Chromium uses 1 iteration for every Linux key (basic and keyring); 1003 is macOS-only. So
the Secret Service/KWallet branch almost certainly fails the same way. That part is **inferred,
not live-tested** (no keyring here; `docs/backends.md` already says the Linux path was
mock-only). The no-keyring case is not "unencrypted": it is AES with the public fixed key
`peanuts`. That is still effectively unprotected, so the docs' warning stands, but the code path
is wrong. Not fixed here (spike, no production code). Filed as mdostal/portunus#266.

## What Stories 02-05 should take from this

1. **Driver A (extension + `credentials.store`)**: inject in the **ISOLATED** world, no gesture
   needed, origin tab required. The §3 main-world hard gate is resolved for CfT/Linux: neither
   fallback (a) nor (b) is needed. The outcome enum cannot distinguish stored/dismissed (F1, F1b),
   so drop `stored`/`dismissed` from the extension reply (e.g. `called` / `error:<code>`) and
   decide only by read-back.
2. **Consent moves to Portunus.** Chrome will not prompt on update (F1), so
   `portunus browser update` must take an explicit human confirmation (or `--yes`) before
   delivery. For a username Chrome does not yet have, the "Save password?" click still applies.
3. **Driver B (Portunus-owned profiles)** can skip the origin page and the bubble entirely and
   use `passwordsPrivate.addPassword` / `changeCredential` via CDP on `chrome://password-manager`
   (F6). That makes design Q1 option (b) cheaper than the design assumed.
4. **Driver C (CSV)**: there is a conflict step and a native file dialog (F5) on top of the
   plaintext-on-disk cost. That strengthens the "drop" recommendation for Q3.
5. **Fix F7 before Story 05 on Linux**, or Linux verify cannot work. Story 02's preflight should
   pick the `origin_url` from `list_logins()`, not rebuild it: F1 showed Chrome updates a row whose
   `origin_url` has a different path from the calling page. Also, `read_password_blob` uses
   `LIMIT 1`, so verify should also assert the row count stayed at 1.
6. **Re-verify on Chrome stable and macOS** (F1, F2, F5) before driver A ships to real profiles.
   Separately, branded Chrome stable is reported to ignore `--load-extension` (137+). That is
   **not verified here** and matters only for test harnesses, not for an operator-installed
   unpacked extension.

## Acceptance criteria, one by one (Story 01)

Per "do not re-plan", the ACs were not edited. These three are **not fully met as written**, for the
reasons given:

| AC | Status |
|---|---|
| 1. update semantics, "verified by re-reading through ChromeBackend", digests only | **Behaviour recorded (F1).** The **ChromeBackend** part is **not met**: `ChromeBackend.access()` fails on every Linux row (F7). It was run on every row and the errors are recorded in `vr-*.json`. The digest proof used a harness-local decrypt of a read-only copy instead. This AC wording is wrong for Linux until F7 is fixed. |
| 2. ISOLATED vs MAIN + hostile page | **Met** (F2). |
| 3. gesture | **Met** (F3). |
| 4. local + account-store row, which store changed | **Not met.** No Google sign-in was available in the runtime, so no account-store row could be created (F4). Carried forward. |
| 5. "Chrome stable's" CSV import conflict | **Behaviour recorded on Chrome for Testing 153, not Chrome stable**, via the API behind the Settings button (F5). Chrome stable is not installed in the runtime. |
| 6. CDP bubble acceptance without OS-level UI automation | **Met** (F6). Answer: no CDP domain, and the bubble is not reachable. Updates need no acceptance, and `passwordsPrivate` writes with no UI. |
| 7. design §3/§6 and research-brief tags | **Met.** Both docs carry `[spike]` notes. One brief item is carried forward (1c.4 account store), and 1a is deliberately untested. |
