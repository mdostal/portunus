# Throwaway spike harness (PANT-1422, Story 01)

**Not product code.** It exists only to reproduce `docs/spike-results.md`. Do not import it
from `src/`, and do not run it against a real profile. Every script creates or takes a
throwaway `--user-data-dir` and uses dummy credentials. Only 16-hex SHA-256 prefixes are
printed.

Deps (not committed): `npm i playwright-core ws` in a sibling dir, plus Playwright's
`chromium-1243` (Chrome for Testing 153). `python-xlib` and `pillow` are needed only for
`xtype.py` / `shot.py` (Xvfb screenshots and typing into the native file dialog).

| file | what |
|---|---|
| `site/index.html`, `site/hostile.html` | test origin pages (`python3 -m http.server 8765` in `site/`). `hostile.html` patches `navigator.credentials.store` / `PasswordCredential` and records what it sees |
| `ext/` | MV3 test extension: `sw.js` runs `scripting.executeScript` in ISOLATED or MAIN world; `main_capture.js` is the `document_start` MAIN-world native capture |
| `seed.mjs` | seeds a row through `chrome.passwordsPrivate.addPassword` on `chrome://password-manager` (Chrome's own write path) |
| `run.mjs <mode>` | one experiment: seed OLD, deliver NEW by `<mode>`, dump call outcome + `password-manager-internals` lines, leak-check the log |
| `pp.mjs` | `passwordsPrivate` `changeCredential` driver test (F6) |
| `imp.mjs` | CSV import conflict / `continueImport` test (F5). CSV 0600 in a 0700 dir, unlinked in `finally` |
| `probe1.mjs` | CDP protocol domain listing and `passwordsPrivate` key listing |
| `verify.py <portunus-src> <udd> <user>` | read-back: copies `Login Data*`, opens read-only, prints row metadata + digests, and the `ChromeBackend.access()` result (F7) |
| `results/` | `res-<mode>.json` (call + internals) and `vr-<mode>.json` (read-back) per run, plus screenshots |

There are no raw `Login Data` writes anywhere in this directory.
