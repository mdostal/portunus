# Design Discussion: portunus-l2-plugin-lifecycle

## 1. What Are We Doing?

Making Portunus's already-shipped UI runnable as a real, host-supervisable service, per
`pantheon-v2`'s actual L2 contract (researched directly, see research-brief.md): a `/api/health`
route returning `{"status":"ok"}`, a documented capabilities list, and `next.config.mjs`
`output: "standalone"` so `node .next/standalone/server.js` is a single, fixed entrypoint the
host's `start: node` supervision model can invoke — matching how Janus/Consus/Mnemosyne are
already run. Registering these facts in `pantheon-v2`'s shared manifests is a separate,
follow-up PR against that repo (see research-brief.md's scope split), not part of this epic.

## 2. What I Found

Covered in research-brief.md. The load-bearing facts: the host derives the L2 descriptor from
plain fields on Portunus's own `pantheon.gods.yaml` entry (no descriptor file needed here),
port 7802 is already claimed by `manifest.json` and doesn't collide with the shared table, and
Next.js's `output: "standalone"` build mode is the standard way to get a single supervisable
entrypoint from a Next.js app (well-documented Next.js feature, not something Pantheon-specific
to invent).

## 3. My Proposed Approach

Single vertical slice:

- **`/api/health`** — new route, `GET` returns `{"status": "ok"}`, HTTP 200. Reads nothing,
  writes nothing, never touches `portunus` subprocess dispatch (unlike every other route) —
  it's a pure liveness signal for the Next.js process itself.
- **Capabilities list** — a small constant (`lib/capabilities.ts` or inline in `manifest.json`)
  documenting what Portunus offers a host: `secret-lookup`, `secret-injection`, `secret-vault`,
  `session-vault` (matches the CLI/UI surfaces already shipped this session).
- **`next.config.mjs` gains `output: "standalone"`** — verified by actually running
  `npm run build && node .next/standalone/server.js` and curling `/api/health`, not just
  trusting the Next.js docs.
- **`manifest.json` update** — add the capabilities list and confirm the `ui.url` port (7802)
  stays the single source of truth this epic's pantheon-v2 follow-up will read from.

## 4. What Could Go Wrong

- **[medium] `output: "standalone"` changes what files `next build` actually needs at runtime**
  (it copies only traced dependencies into `.next/standalone/`) — if anything the API routes
  need isn't traced correctly, standalone mode could work in dev but fail at the actual
  supervised entrypoint. Mitigation: the verification step above actually runs the standalone
  server and exercises a real API route (not just `/api/health`), not just a build-succeeds
  check.
- **[low] `/api/health` accidentally becomes a dispatch point that shells out to the CLI.**
  Mitigation: keep it maximally trivial — no `runPortunus()` call, just a static JSON response.

## 5. Dependencies and Constraints

None beyond what's already shipped. Zero Python-side changes.

## 6. Open Questions

None for scope 1. Scope 2 (the pantheon-v2 registration PR) has its own open question: what
`phase`/`status` value is honest to claim given I can't verify the host's install/supervise
loop actually picks Portunus up on the real box from within this session — resolved as "don't
claim built/live, only register the real facts (health endpoint, capabilities, port) and leave
phase/status conservative," presented to the operator for their own call.

## 7. Verification Strategy

```
VERIFICATION PLAN:
  Tools: npm run build, a real standalone-server smoke test (not just build-succeeds)
  Automated: none new at the Python level (no Python changes)
  Manual: npm run build && node .next/standalone/server.js, curl /api/health and one real
    API route (e.g. /api/registry) against a live PORTUNUS_HOME to confirm standalone mode
    doesn't break existing routes. (Grill finding, resolved) Next.js standalone output does
    NOT auto-include public/ or .next/static/ -- manually copy both alongside the standalone
    server per Next's own docs, then fetch the actual HTML page and confirm the CSS bundle
    loads, not just an API route (an API-route-only check wouldn't catch missing static assets)
  Not verifying: actual host-side supervision on the real Pantheon box (out of scope --
    that's the pantheon-v2 follow-up PR's concern, and even then only observable on the box)
```

## 8. Scale Assessment

```
SCALE ASSESSMENT:
  Files affected: 3 (next.config.mjs, new /api/health route, manifest.json)
  Subsystems: UI build config + one new trivial route
  Migration required: no
  Unknowns: 0 (scope 1)

  RECOMMENDATION: Proceed directly to stories (Small scope)
  RATIONALE: Small, well-understood change; the real risk (standalone mode breaking existing
    routes) is directly testable by actually running the standalone server, not just building.
```
