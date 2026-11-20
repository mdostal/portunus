# Design discussion: Chrome saved-login write-back / rotate

Status: **proposed. The Story 01 spike has landed (`spike-results.md`, Chrome for Testing 153 / Linux);
it is still awaiting operator decisions (§6).** The spike invalidated one premise of this design: Chrome does
not ask before updating a saved password (F1). The affected text below is marked
**[spike]**. Stories 02-06 still say what they said; where a story's AC now conflicts with a finding,
that is listed in `spike-results.md` and on PANT-1422. Nothing was silently rewritten.

## 1. Shape

Write-back is a **rotation sink**, not a new backend. `ChromeBackend` stays read-only. A new
`chrome_writeback` module takes "set Chrome's saved password for
`chrome:<profile>:<origin>:<username>` to the value of Portunus reference `X`". It delivers that
value through **Chrome's own write path** and then proves the write landed, by re-reading it
through the existing read path and comparing SHA-256 digests.

```
 new value (any source: rotation adapter output, or a human `portunus drop`)
        |
        v
 portunus browser update <chrome-ref> --from <portunus-ref> [--dry-run]
        |
   plan + preflight  (metadata only: row exists? Chrome running? account-store row? store type)
        |
   delivery driver   (resolves --from at the boundary, hands it to Chrome's own write path)
        |             - driver A: companion extension + native-messaging host -> credentials.store()
        |             - driver B: CDP into a Portunus-owned --user-data-dir profile (test profiles)
        |             - driver C: CSV import tempfile (fallback, off by default)
        v
   verify            (ChromeBackend.access() digest == digest of --from value; neither printed)
        |
   audit             ("chrome.writeback" entry: ref names + result, never a value)
```

Chrome's own confirmation ("Update password?") is **kept, not bypassed**. A human click on
Chrome's own bubble is the consent step. Portunus never suppresses or auto-clicks it.

**[spike F1] This premise does not hold for updates.** On Chrome for Testing 153 / Linux,
`credentials.store` for a username Chrome already has is applied **immediately**, with only a
"Password updated" notice afterwards and no Update/Cancel choice. The click exists only for a *new*
username ("Save password?"). Consent for an update therefore has to be taken **by Portunus before
delivery** (proposed Q5). Not yet re-checked on Chrome stable or macOS.

## 2. Pieces

- **`chrome_writeback.plan(target_ref, source_ref) -> WritePlan`**. Pure, metadata-only. It
  parses the `chrome:` sm_name the same way `ChromeBackend.access()` does, with
  profile-first/username-last splitting so ported origins survive. It confirms the row exists via
  `chrome_discovery.list_logins()`, reports whether Chrome is running (`_process_running`), and
  flags an account-store (`Login Data For Account`) match. Like `chrome_discovery`, this module is
  **structurally incapable** of touching a password. It never imports `chrome_crypto` and never
  resolves `source_ref`. `portunus browser update --dry-run` prints the plan.
- **Native-messaging host (`portunus browser host`)**. A Portunus entrypoint Chrome launches over
  stdio. It speaks Chrome's framing (4-byte little-endian length + JSON), and only to the
  extension ID pinned in its host manifest (`allowed_origins`). `portunus browser install-host` /
  `uninstall-host` writes/removes the per-platform host manifest. Exactly one message type
  carries a value (`deliver`), it is sent once, and it is never logged.
- **Companion MV3 extension (`extensions/chrome-writeback/`)**. Holds no secrets at rest. On
  `deliver` it focuses or opens a tab on the target origin. It calls `navigator.credentials.store`
  in whichever world Story 01 proves safe (§3) and replies with an outcome enum (`stored`,
  `dismissed`, `error:<code>`), never the value. **[spike F1/F1b/F2]** Use the ISOLATED world. `store()`
  resolves successfully whether Chrome saved, updated, or left a "Save password?" bubble pending, so
  `stored`/`dismissed` cannot be told apart in the extension. The reply can only be `called` /
  `error:<code>`, and the verify step decides the outcome.
- **CDP driver (test profiles)**. Only for a Portunus-owned `--user-data-dir`. The Chrome 136 lock
  makes it impossible on default profiles anyway. Same `credentials.store` call through
  `Runtime.evaluate`, with the same outcome enum. It is built only if the operator picks
  test profiles in Q1. **[spike F6]** A simpler route was measured: CDP on
  `chrome://password-manager` can call the WebUI's own `chrome.passwordsPrivate.addPassword` /
  `changeCredential`. That writes through Chrome's store headless, with no origin page and no bubble.
  `requestCredentialsDetails` puts the current plaintext into that page's JS context, so the driver
  must keep it there and return nothing but an outcome.
- **Verify + audit**. **[spike F7]** `ChromeBackend.access()` cannot decrypt any Linux row today
  (1003 vs 1 PBKDF2 iteration; the no-keyring v10 blob is treated as plaintext), so verify on Linux
  depends on that bug being fixed first. It also needs a row-count check, because `read_password_blob`
  is `LIMIT 1`. Reuses `ChromeBackend.access()` for the read-back and compares
  `sha256(readback) == sha256(source)` in-process. Only the boolean leaves the function. Uses the
  existing `AuditChain.append(action, secret, result)` with `action="chrome.writeback"` and
  `secret=<target ref name>`.
- **Rotation wiring**. A rotation run can name a Chrome reference as an extra sink
  (`--also-update-chrome <chrome-ref>`). The adapter stores its new value as usual, then write-back
  runs against it. Failure of the Chrome sink never rolls back the provider rotation. The provider
  credential already changed, so Chrome failure is reported as "Chrome copy stale" with the exact
  `portunus browser update` command to retry.

## 3. Security invariants (same bar as every Portunus backend)

- A delivered value appears in exactly three places, each in memory, for one call. They are the
  resolver boundary sink, the one `deliver` native-messaging frame, and the
  `credentials.store` argument. Never in a log, print, exception message, audit entry, CLI
  output, extension `console`, `chrome.storage`, or the outcome reply.
- **Main-world exposure is a hard gate.** If Story 01 shows `credentials.store` only works from
  the page's main world, page scripts on the origin could intercept it. That includes
  third-party analytics. The design then requires **one** of: (a) run in a fresh tab before any
  page script, via `document_start` plus capture of the native `navigator.credentials.store`
  before the page can patch it, proven by a test page that tries to patch it; or (b) fall back
  to the CDP driver or the CSV fallback for that origin. If neither (a) nor (b) holds, driver A
  does not ship. This is decided after the spike, not hand-waved now.
  **[spike F2] Resolved for Chrome for Testing 153 / Linux: `credentials.store` works from the ISOLATED
  world.** Neither (a) nor (b) is needed. Driver A **must** use `world: 'ISOLATED'`. The hostile test
  page proved that a plain MAIN-world call leaks the password to a page patch. Option (a) dodged that
  page's patches but stays fragile (`Function.prototype` and the `document_start` race), so it is not
  recommended. F3: no user activation is needed. Re-check on Chrome stable / macOS before
  shipping (not verified).
- **[spike F1] Chrome does not gate updates.** Any script on the origin, including the site's own
  scripts, can overwrite a saved password for a known username without a click. This is Chrome's
  behaviour, not something Portunus adds. But it means Portunus cannot cite Chrome's bubble as consent:
  `portunus browser update` must require explicit human confirmation (or `--yes`) and must audit it.
- Host manifest `allowed_origins` pins one extension ID. The host rejects any other caller and
  any message type it does not know.
- **No raw `Login Data` writes**, ever, by any driver. A structural test asserts no module in
  `chrome_writeback`/host code opens `Login Data*` in a write mode or issues
  `INSERT`/`UPDATE`/`DELETE` against it. This mirrors the read epic's
  `set_trace_callback` test (lessons-learned: `sqlite3.Cursor.execute` is immutable on 3.13, so
  patch `sqlite3.connect`).
- CSV fallback (driver C), if built at all **[spike F5: on a matching row the import returns
  `CONFLICTS` and imports nothing; replacing needs a second `continueImport` step, and the picker is a
  native OS dialog that CDP cannot drive]**: off unless `--allow-plaintext-csv`, 0600 file in a
  0700 private tempdir, hard TTL, unlink in `finally`, and a loud warning. Test proves unlink on
  every exit path, exception included.
- Live proofs compare digests only, as in the read epic.

## 4. Testing strategy

- **Unit (pytest, TDD):** plan/preflight against synthetic `Login Data` fixtures, reusing the read
  epic's fixture builders. Native-messaging framing round-trip and rejection cases. Host manifest
  install/uninstall into a temp HOME. Verify step with a stubbed `ChromeBackend`. Audit entry
  shape. The structural no-leak and no-raw-write tests.
- **Extension:** a small JS test harness for the message handler (outcome enum, never echoes
  value). Whether it lives under `ui/`'s npm tooling or standalone is decided in Story 04.
- **Live (spike + closeout):** a throwaway Chrome profile only, never the operator's real
  profile without explicit go-ahead. Digest-only proof. If a platform could not be live-tested,
  say so (the read epic's Linux gap is the precedent).

## 5. Self-grill

**Q: Isn't this just the read epic's "inject" done through a browser?**
A: No. Read-back only ever exposes values to Portunus's boundary sinks. This epic is the first
where Portunus deliberately hands a value to *another application* (Chrome) for persistence.
That is why delivery is limited to Chrome's own consent-gated write path, and why the
main-world question (§3) is a hard gate.

**Q: Why not just write the SQLite row while Chrome is closed? It's simpler.**
A: The issue rejects it. Beyond the race, it skips sync and account-store bookkeeping
(research-brief §1a) and makes Portunus a second, undocumented writer of Chrome's schema, which
breaks silently on any Chrome schema change. Not revisited.

**Q: Does keeping Chrome's "Update password?" click defeat automation for test scenarios?**
A: For real profiles, the click is the consent step and stays. For automated test scenarios, the
answer is Q1's test-profile option. Still unverified there: whether the bubble can be accepted in
a Portunus-owned profile without UI automation. Story 01 checks. If it cannot, test profiles may
be better served by *not* storing in Chrome at all and letting the test fill the form from a
Portunus boundary sink.
**[spike F1/F6]** Answered: there is no bubble to accept for an update, and CDP has no password domain
and cannot click the bubble anyway. For a Portunus-owned profile, `passwordsPrivate` over CDP (F6) writes
with no UI at all, so automation is not blocked. For real profiles, see the §1 note: the click this
answer relied on does not exist for updates.

**Q: Does an extension in the operator's daily browser widen attack surface?**
A: Yes. It is a new component that can open tabs and call `credentials.store`. Mitigations are an
ID-pinned host, no secrets at rest, minimal permissions (only `nativeMessaging`, `scripting`,
`tabs`; host permissions requested per-origin at use time, not `<all_urls>`), and an unpacked or
self-distributed install the operator opts into. Flagged for operator sign-off (Q2).

**Q: Site-side password change?**
A: Out of scope (research-brief §2). There is no general API. Per-site automation is unbounded.
Provider-specific adapters already exist in `rotation.py` for services that have APIs.

unresolved_count: 5 (Q1-Q4 + new Q5 below). Of the research-brief §1c spike unknowns, 3 are resolved
for Chrome for Testing 153 / Linux. 1 is carried forward (account store, §1c.4). Chrome stable and
macOS re-checks are open (see `spike-results.md`).

## 6. Decisions needed from the operator before Stories 02+ start

- **Q1. Target:** (a) the operator's real Chrome profiles, (b) Portunus-owned test profiles
  (custom `--user-data-dir`), or (c) both? (a) needs drivers A (extension + host). (b) needs
  driver B (CDP). Recommendation: **(b) first** for the test-scenario use case. It is lower risk,
  fully automatable, and never touches the daily browser. Then (a).
  **[spike F6]** (b) is cheaper than planned: `passwordsPrivate.addPassword` / `changeCredential` via
  CDP on `chrome://password-manager` needs no origin page, no extension and no bubble (CfT 153 / Linux;
  macOS reauth not verified).
- **Q2. Companion extension in the daily browser:** acceptable, given the §5 mitigations?
  Required for (a).
- **Q3. CSV import fallback (driver C):** build it behind `--allow-plaintext-csv`, or drop it
  entirely? Recommendation: **drop**. It is the only path that puts a plaintext password on
  disk. **[spike F5]** This strengthens the case for dropping it: an import onto an existing row stops at
  `CONFLICTS` and needs a second "replace" step, and the file picker is a native dialog that needs
  OS-level UI automation.
- **Q4. Rotation coupling:** is `--also-update-chrome` on rotation runs wanted in this epic, or
  should standalone `portunus browser update` ship first? Recommendation: standalone first
  (Story 05 can be deferred without blocking the rest).
- **Q5 (new, from spike F1). Consent for an update:** Chrome applies updates with no prompt, so who
  consents? Recommendation: `portunus browser update` shows the plan (target ref, source ref, store) and
  requires an interactive confirm or explicit `--yes`, which is recorded in the audit entry. For a
  *new* username, Chrome's "Save password?" click still applies on top of that.
- **Blocker for Story 05 on Linux (spike F7):** fix the `chrome_crypto` Linux key derivation
  (1 iteration; v10 with no keyring = AES with the `peanuts` key). Without the fix, the
  verify-by-read-back step cannot succeed on Linux. Tracked in mdostal/portunus#266.
