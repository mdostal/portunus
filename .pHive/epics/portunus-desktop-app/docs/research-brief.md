# Research Brief — portunus-desktop-app

## 1. Ask

User (verbatim, prior turn this session): *"let it either run as an installed app -- tauri etc
for you to install and use, or you can run in the local browser... but i think this is ready to
ship if we just build the auto-update feature into it as well to allow it to pull updates when
it is full installed as an app, and we can dogfood locally."*

This turn: *"let's plan and get the install -- make sure auto-updates and keep building so i can
use it locally and start getting it across my 50 projects so it can call back and forth."*

## 2. What "across my 50 projects... call back and forth" actually requires

Checked directly rather than assumed: **cross-project secret access already works today, with
zero desktop-app involvement.** The CLI/MCP server default to the same shared
`PORTUNUS_HOME` (`~/.portunus`) regardless of which repo a Claude Code session is rooted in —
confirmed via `paths.py:home()` and every epic this session. Any of the 50 projects' agent
sessions can already `portunus resolve`/MCP-call into the one shared vault right now.

So the desktop app's real, honest value-add is narrower than "enables cross-project access" —
it's **a persistent, native way to run the Vault UI** (menu bar icon, one click to open the
dashboard, survives across terminal sessions) instead of manually `cd ui && npm run dev` every
time, **plus staying current without a manual rebuild**. This brief and the design that follows
scope the epic to that — not a new required chokepoint other tools must go through.

## 3. Environment facts (checked directly, not assumed)

- Rust/Cargo already installed (`rustc 1.97.1`, `cargo 1.97.1`) — no toolchain bootstrap needed.
- Node 22.12.0, existing `ui/next.config.mjs` already builds `output: "standalone"` — a
  self-contained `.next/standalone/server.js`, explicitly built for exactly this
  "an external process supervises this UI" scenario (comment cites the deferred L2 plugin
  epic). This is the natural Tauri sidecar target with zero UI-side rework.
- The UI's API routes shell out to the `portunus` CLI via `child_process.spawn("portunus", ...,
  { env: process.env })` (`ui/lib/portunus.ts`). A Tauri-wrapped GUI process on macOS launches
  with a near-empty `PATH` (no `.zshrc`/`.zprofile` sourcing, a well-documented Electron/Tauri
  gotcha) — `spawn("portunus", ...)` would silently fail to find the pip-installed CLI unless
  the real login-shell `PATH` is captured and forwarded. This is a **real, concrete risk**, not
  hypothetical — must be handled explicitly (§5 below), not glossed over.
- Repo (`mdostal/portunus`) is currently **private** (`gh repo view` confirms). `gh` is
  authenticated locally with `repo` scope (`gh auth status` confirms) — this machine can already
  read private release assets via `gh`, no new credential needed.
- Machine: arm64 (Apple Silicon), macOS 26.5.1. Single machine, single user — this is NOT a
  multi-machine distribution problem.

## 4. Tauri v2 auto-update — the standard path, and why it doesn't fit v1 as-is

Standard Tauri auto-update (`tauri-plugin-updater`): a signing keypair (`tauri signer
generate`) signs release artifacts; `tauri.conf.json` embeds the public key and an
`endpoints` URL for a `latest.json` manifest; the running app polls that URL, verifies the
signature, downloads, and swaps itself. The standard hosting choice is a **public** GitHub
Release (`tauri-action` uploads `latest.json` + binaries automatically on tag push) —
fetching from a *private* repo's release assets requires an authenticated request, which would
mean embedding a GitHub token in the shipped app. That is exactly the credential-handling
anti-pattern this whole project exists to prevent (a live secret baked into a distributed
binary) — not an acceptable design here regardless of how low-stakes it might seem for a
single-user tool.

Two real ways to resolve this: (a) make the repo public now (already planned as this session's
own future "Phase 3", just not yet done), unblocking the standard public-feed flow with zero
embedded credentials; or (b) keep the repo private and have the *already-installed, already-
authenticated* `gh` CLI do the update check/download instead of the shipped app polling a
public endpoint — no token ever enters the app bundle, because the app never holds one; it
shells out to `gh`, which holds the user's own credential exactly the way `portunus` itself
already treats every other credential (never touched directly, always delegated to the
boundary tool that already has it).

**Chosen for v1: (b).** It works today without a repo-visibility decision, needs no Apple
Developer Program membership, and matches this project's own credential-handling posture more
closely than embedding a token would. Repo-publicity + the full signed-feed pipeline stays a
clean, compatible upgrade path for later (documented, not built this epic) — switching from "gh
CLI checks/downloads" to "Tauri polls a public feed" only replaces the update-source
implementation; the rest of the app (window, tray, sidecar) is unaffected either way.

## 5. macOS code signing — ad-hoc is sufficient for this scope

Notarization (bypassing Gatekeeper's "unidentified developer" warning for *everyone* who opens
the app) requires a paid Apple Developer Program membership ($99/yr) and CI-side signing
secrets. That solves a distribution problem this epic doesn't have: this is **one user, one
machine**, not a public release to strangers. Ad-hoc signing (no Apple identity at all, still
required on Apple Silicon for any app to run) is sufficient — the one-time "open anyway" bypass
in System Settings → Privacy & Security happens once, at first install, not on every update
(the app updates itself in place rather than being re-downloaded through a browser, so the
quarantine flow that trips repeat prompts largely doesn't re-fire). Documented explicitly as a
v1 scope decision, not an oversight — a public-distribution follow-up epic would need real
notarization.

## 6. Sidecar architecture precedent

Confirmed via research: bundling a Node runtime + an existing Next.js `standalone` build as a
Tauri `externalBin` sidecar, with Rust spawning it and pointing the WebView at
`http://localhost:<port>`, is a documented, common pattern — not a novel approach for this repo
to pioneer. The two real gotchas from that research, both must be handled explicitly:
(1) the near-empty-`PATH` issue (§3) — capture the user's login shell `PATH` before spawning
anything; (2) port coordination — pick a free port at startup (not hardcode 3000, which may
already be in use, as it literally was during this session's own live testing) and pass it to
both the sidecar and the WebView consistently.

## 7. Scope boundary for this epic

**In scope:** Tauri v2 desktop app wrapping the existing Next.js UI as a sidecar; macOS
(arm64) only; ad-hoc signing; menu-bar/tray presence; single-instance enforcement; `gh`-CLI-
backed update check + self-update (manual trigger + periodic background check); local build +
install docs; version bump.

**Out of scope, explicitly deferred:** Windows/Linux builds; Apple notarization / Developer
Program membership; making the repo public (compatible future upgrade, not required here);
bundling the Python CLI itself as a sidecar (the CLI stays a separately pip-installed
dependency, exactly as the UI already assumes today); any new "desktop app as required
chokepoint" architecture — cross-project access via CLI/MCP is unaffected and unchanged by this
epic.
