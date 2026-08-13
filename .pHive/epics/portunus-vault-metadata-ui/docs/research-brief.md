# Research Brief: portunus-vault-metadata-ui

## Requirement

Surface everything shipped in `portunus-vault-metadata` (v0.7.0) in the standalone UI — the
user's stated primary UAT surface ("that's where all my feedback for what needs to exist came
from"). Explicit scope boundary from the user: no `pantheon-v2`/other-repo/L2-integration work
in this epic — standalone UAT inside this one repo only.

1. Metadata display + edit — `description`/`purpose`/`injected_as` currently invisible in the UI.
2. A discovery panel — `portunus discover --provider gcp --project <id>` in the browser.
3. A "list keys for project" view — the visual equivalent of `portunus list --project X`.
4. GCP project-binding visibility (read-only, presence-only — never the audience value).

## A real prerequisite gap found during research (not in the original ask)

Grepped the CLI: **neither `reg add` nor `drop` expose `--description`/`--purpose`/
`--injected-as`**, even though `Registry.add()` has accepted them since v0.7.0's story 01.
`Registry.retag()` (the only in-place-update path) doesn't accept them either — only
`provider`/`project`/`env`/`tags`. Today the *only* code path that ever sets these fields is
`discover.py`'s auto-seed-from-GCP-labels. There is no human-facing way to set or edit
description/purpose/injected_as at all. This has to be fixed first — the UI can't expose an
edit form for fields the CLI itself can't write.

## Current state (verified against code)

- Every UI API route (`ui/app/api/{registry,audit,ask,inject,drop,retag,health}/route.ts`) is a
  thin wrapper around `runPortunus(args, stdin?)` (`ui/lib/portunus.ts`) — spawns the real
  `portunus` binary, never reimplements gating/business logic in TypeScript. This is
  non-negotiable and every new route in this epic follows it exactly.
- `PortunusReference` is defined **twice** — once in `ui/lib/portunus.ts`, once in
  `ui/app/types.ts` — a pre-existing duplication (not introduced by this epic). Both need
  `description`/`purpose`/`injected_as` added to stay in sync.
- `/api/registry` (`GET`) already returns every `Reference.to_dict()` field via `portunus reg
  json` — description/purpose/injected_as will appear in its response automatically once the
  TS type is updated; **no Python change needed for the read path**.
- `/api/retag` (`POST`) shells to `portunus retag`, passing only `provider`/`project`/`env`/
  `tags` — needs `--description`/`--purpose`/`--injected-as` flags added to both the CLI
  subcommand and this route once `Registry.retag()` supports them.
- `DetailDrawer.tsx` already has a working "Move" pattern: an expandable form
  (`moveOpen`/`moveDraft` state) that POSTs to `/api/retag` and reuses the CLI's own collision
  error verbatim. The natural, minimal-new-surface way to add metadata editing is extending
  this same form/handler with description/purpose/injected_as fields, not building a second
  edit surface.
- `page.tsx` has a two-tab shell (`Tab = "console" | "map"`) plus a persistent `AskBar` side
  panel. Adding a third tab is a one-line type-union change plus a nav button, matching the
  existing pattern exactly.
- No JS/TS unit-test framework exists in this repo (`project-profile.yaml` →
  `test_infrastructure.e2e`: "none yet — Playwright integration is planned"). Every prior UI
  story this session (session-vault UI, rotation indicator, L2 lifecycle) verified via
  `npm run build` (TypeScript correctness) + a live smoke test (curl / Playwright screenshot),
  not unit tests — this epic follows the same convention for its TSX stories. Python
  (registry.py/cli.py) stories still follow full TDD (pytest), matching the established split.
- `next.config.mjs`'s `output: "standalone"` build still needs the manual
  `cp -r .next/static .next/standalone/.next/static` step for a fresh smoke test (documented in
  the file's own comment, learned the hard way earlier this session).

## Constraints (cross-cutting-concerns.yaml)

`secret-boundary-invariant` applies with a specific new angle this epic: `description`/
`purpose`/`injected_as` are NOT secret values (safe to display/edit freely), but the discovery
panel's register action and the GCP-binding-presence indicator must not introduce a second way
to reach a value or a WIF audience string in the browser — the panel shows presence/absence
only, matching `portunus auth gcp`'s own restraint (identity/scope/expiry only, never the
audience or token). `audit-chain-integrity` applies to any new write path (metadata edit,
discovery register) exactly as it already does to retag/drop.

## Scope decision for this pass

- Metadata edit reuses the existing Move form/retag mechanism — no second edit surface.
- Discovery register action calls the CLI's own `--register` (state=requested, never-overwrite,
  already fully safety-reviewed in the backend epic) — the UI never reimplements that logic.
- GCP-binding visibility is presence/absence only (boolean: "WIF configured" y/n per project),
  folded into the same new project-explorer surface as the list/discover views rather than a
  separate settings panel — it's inherently project-scoped, same as list/discover.
