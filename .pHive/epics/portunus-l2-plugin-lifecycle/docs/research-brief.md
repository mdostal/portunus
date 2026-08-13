# Research Brief: portunus-l2-plugin-lifecycle

## Requirement

Wire Portunus into the Pantheon host's L2 plugin lifecycle (deferred since kickoff as
secondary to standalone-first). Requires understanding the actual host contract, researched
directly against `mdostal/pantheon-v2` (cloned read-only, not vendored into this repo).

## What I found in pantheon-v2 (source of truth: `docs/PANTHEON-CONTRACTS.md`, `lib/gods-adapter.ts`)

- Portunus is **already registered** in `pantheon.gods.yaml` (`phase: spec`) and
  `plugins.manifest.yaml` (`status: spec, start: none, shape: service`) — someone already
  declared the intent, but with no health endpoint, capabilities, or port populated.
- The host's `gods-adapter.ts` **derives the L2 `ServiceDescriptor` directly from fields on
  the `pantheon.gods.yaml` entry itself** (`health_endpoint`, `capabilities`, `api_version`,
  `port`, `transport`) when `shape: "service"` — there is **no separate descriptor file
  required in the god's own repo**. If `health_endpoint` is absent, no descriptor is built at
  all (confirmed by reading `mapGodEntry()`'s conditional).
- `docs/PORTS.md`'s shared port table has **no entry for Portunus** — but Portunus's own
  `manifest.json` already declares `"ui": {"url": "http://localhost:7802", ...}`, and 7802
  doesn't collide with any port already claimed in that table (8090, 3010, 6343, 6344, 3011,
  8091, 8726, 8722, 8477, 4870).
- **Notably, none of the currently "live"/"built" gods (Consus, Mnemosyne) have actually
  populated the L2 descriptor fields yet** — their ports only appear in free-text `notes:`,
  parsed by an undocumented regex fallback (`extractPortFromNotes`). Populating the real
  fields properly would make Portunus the first god to dogfood the documented contract as
  written, not "how everyone already does it."
- Health endpoint convention (`docs/plugin-contract.md` §3, `PANTHEON-CONTRACTS.md` §2a):
  HTTP 200, JSON body `{"status": "ok"}` at minimum. A dedicated `/health` route is preferred
  over reusing an existing page when the god author controls the code (Portunus does).
- Portunus's UI (`ui/`, shipped in `portunus-standalone-core`) is already a Next.js app with
  `/api/*` routes — the natural place for a health route. For the host to supervise it as
  `start: node` with a fixed `entrypoint`, the app needs a single runnable server script; Next.js
  ships one automatically when `next.config.mjs` sets `output: "standalone"` (produces
  `.next/standalone/server.js`, honors the `PORT` env var).

## Scope split (two repos, two different risk profiles)

1. **This repo (portunus)** — build the actual capability: a `/api/health` route, a
   capabilities list, and `output: "standalone"` so the UI can run as a real supervised
   service, not just `npm run dev`. Fully within this session's established autonomy (tested,
   verified, shipped through the normal flow).
2. **`mdostal/pantheon-v2`** (a different repo, shared by every other god) — register
   Portunus's real `health_endpoint`/`capabilities`/`api_version`/`port`/`transport` on its
   existing `pantheon.gods.yaml`/`plugins.manifest.yaml` entries, and add it to
   `docs/PORTS.md`. This touches shared infrastructure other gods/people depend on and isn't
   verifiable as "actually running/supervised" from within this session (I can't confirm the
   host's install/supervise loop picks it up on the real box) — prepared as a PR for operator
   review, not auto-merged, unlike this repo's own release flow this session.

This epic covers **only scope 1**. Scope 2 is a follow-up PR against `pantheon-v2`, presented
separately.
