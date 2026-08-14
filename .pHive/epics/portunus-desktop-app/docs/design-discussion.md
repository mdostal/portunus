# Design Discussion — portunus-desktop-app

## 1. Shape

A Tauri v2 shell wraps the existing Next.js `standalone` build as a sidecar process, shows it
in a native window with a menu-bar/tray presence, and self-updates by shelling out to the
user's own already-authenticated `gh` CLI — no new server, no embedded credential, no new
required chokepoint for the CLI/MCP surfaces that already work across every project today.

```
┌─────────────────────────── Portunus.app ───────────────────────────┐
│  Rust (src-tauri/)                                                  │
│   1. capture real login-shell PATH (GUI-launched procs get ~empty)  │
│   2. bind port 0 -> OS-assigned free port                           │
│   3. spawn `node <resource_dir>/web/server.js` with PORT=<port>,    │
│      PATH=<captured>                                                 │
│   4. poll http://127.0.0.1:<port>/api/health until 200               │
│   5. open window at that URL; tray icon (Open / Check for Updates /  │
│      Quit); close-to-tray, Quit kills the sidecar explicitly         │
│                                                                       │
│   node server.js (Next.js standalone, bundled as a Tauri resource)   │
│    └─ /api/* routes shell out to `portunus` (unchanged from today)   │
└───────────────────────────────────────────────────────────────────┘
                    │ update check (gh CLI, user's own auth)
                    ▼
       gh release view/download --repo mdostal/portunus latest
```

## 2. Sidecar, not a bundled Node runtime — a deliberate v1 simplification

Tauri's usual "bundle a binary as an `externalBin`" path exists to make an app portable to
machines that don't already have the runtime. That doesn't apply here: this is one user's own
machine, which already has Node 22 (nvm-managed) and the `portunus` CLI (pip-installed) present
— confirmed in the research brief, not assumed. v1 spawns the *system* `node` (resolved via the
captured `PATH`) against a Next.js standalone build **copied into the app's bundled resources**
at build time (`src-tauri/resources/web/`, via Tauri's resource-bundling — not referencing the
git checkout path, so the installed `.app` keeps working even if the repo directory is later
moved or deleted). Bundling Node itself as a true sidecar binary is a clean, compatible future
upgrade (needed only if this app is ever handed to someone else's machine) — not built here.

## 3. The two real risks from the research brief, handled explicitly

**PATH capture.** Before spawning `node`, Rust runs the user's login shell non-interactively
(`$SHELL -ilc 'echo -n $PATH'`, falling back to `/bin/zsh` if `$SHELL` is unset) with a bounded
timeout, and uses that resolved `PATH` — not the GUI process's own near-empty one — as the env
for the sidecar. The sidecar's `node` process inherits that same env for every
`child_process.spawn("portunus", ...)` call in `ui/lib/portunus.ts` (unchanged code, no
edits needed there — this is purely an env-propagation fix at the Rust boundary).

**Port collision.** Never hardcode 3000 (it was already in use during this session's own live
testing). Bind to port `0`, let the OS assign a free one, pass it to the sidecar via `PORT` (Next
.js standalone respects this natively) and to the WebView URL. Poll `/api/health` before showing
the window, so a slow-starting sidecar shows a native loading state instead of a browser error
page.

## 4. Update mechanism: `gh` CLI, not Tauri's built-in updater plugin

Tauri's standard updater (signing keypair + `latest.json` feed) assumes a **publicly fetchable**
release endpoint — the repo is private today (research brief §4), and embedding a GitHub token
in the shipped app to read a private release is the exact credential-in-a-binary anti-pattern
this project exists to prevent. Instead: the app shells out to `gh`, which already holds the
user's own credential the same way `portunus` itself never touches a credential it doesn't have
to.

Flow: `gh release view --repo mdostal/portunus latest --json tagName` (background, every 6h
while the app is open, plus a manual "Check for Updates" tray item) → compare against the
running app's own version (`tauri.conf.json`, kept in lockstep with the Python package version
— one version number across the whole repo, same discipline every prior epic used) → if newer,
a native dialog asks to install, **never a silent unattended swap** (replacing a running app is
consequential enough to ask, not force). On confirm: `gh release download <tag> --pattern
'portunus-desktop-*.zip'`, then a small bundled **relauncher script** (not the running app
itself — a process can't safely overwrite its own binary while running) does the actual swap:
copy the new `.app` to a temp path, sanity-check it (`Info.plist` exists, `codesign -dv`
succeeds), `mv` the running `.app` to `.app.bak`, move the new one into place, delete `.bak` only
on success — restore from `.bak` on any failure. The app quits, the relauncher (already spawned,
detached) does the swap, then relaunches `open /Applications/Portunus.app`.

This is a real design decision to record: **"auto-update" here means auto-*checked*, one-click-
confirmed, not silently forced** — matches this project's general posture of never taking a
consequential action without a clear signal, and is honestly better UX for a vault app the user
doesn't want restarting under them mid-task.

## 5. Signing: ad-hoc, not notarized — scoped explicitly, not an oversight

One user, one machine. Ad-hoc signing (`signingIdentity: "-"`, no Apple identity, no cost, no
CI secrets) is sufficient — required on Apple Silicon for any app to launch at all, and the
Gatekeeper "unidentified developer" bypass (System Settings → Privacy & Security → Open Anyway)
happens once, at first install, documented plainly in the README rather than left as a
surprise. Full notarization (paid Apple Developer Program) is the correct next step **only if**
this ever ships to someone else's machine — explicitly deferred, not this epic.

## 6. Release packaging

New `.github/workflows/release-desktop.yml`, triggered on the same `vX.Y.Z` tag push this
project's existing `gh release create` flow already produces (one version number, one release,
covering the Python package AND the desktop app — no second parallel versioning scheme).
`macos-latest` GitHub-hosted runners are Apple Silicon by default, matching this machine's own
architecture. The job runs `tauri build` (ad-hoc signed, no secrets needed) and uploads the
zipped `.app` as a release asset alongside whatever `gh release create` already attaches.

## 7. Known limitations, recorded rather than silently accepted

- **Orphaned sidecar on a hard crash.** Tauri's single-instance plugin prevents *double-launch*
  collisions, but a force-quit/crash of the Tauri process could leave the spawned `node`
  process running. Because each launch picks a *fresh* free port rather than a fixed one, an
  orphan doesn't block a new launch — it just idles until the machine restarts. Acceptable for
  v1; not a data-safety issue (the orphaned server still only proxies to the local vault the
  same way a normal launch would). A `pkill -f "server.js.*portunus"` note goes in the README
  as a manual escape hatch, not built as an automated cleanup this epic.
- **Windows/Linux, notarization, bundling the Python CLI itself, and a public-repo signed
  update feed** are all explicitly out of scope (research brief §7) — each is a real, separable
  follow-up, not a gap in this epic's own acceptance criteria.

## 8. Self-grill — resolved before writing stories

- *Could the relauncher script leave the app half-swapped on failure?* Addressed in §4:
  copy-then-verify-then-atomic-rename-with-backup, not an in-place overwrite. This is an
  explicit acceptance criterion on story 03, not an implementation detail left to chance.
- *Does this create a second, competing way to run the UI?* No — `cd ui && npm run dev` stays
  fully valid (CI, non-desktop use). The Tauri app is additive, not a replacement path.
- *Does this change how the 50 other projects reach the vault?* No — confirmed in the research
  brief, cross-project access already works via the CLI/MCP against the one shared
  `PORTUNUS_HOME`, unchanged by this epic. The desktop app is a nicer way to *see* the vault,
  not a new requirement to *use* it.

## 9. Scale assessment

**Medium-large.** New Rust project + CI workflow + a real (if small) self-update mechanism with
a genuine failure-safety requirement. Four stories, `classic` methodology (packaging/glue work,
not TDD-shaped business logic) except story 03's version-comparison function, which gets a real
Rust unit test since it's pure, testable logic. Proceeding to story decomposition — no
ambiguous product decision found that needs to block on the user.
