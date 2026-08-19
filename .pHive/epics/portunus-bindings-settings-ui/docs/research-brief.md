# Research Brief — portunus-bindings-settings-ui

## 1. Ask

User: *"keep going on the next epic"* (open-ended, following the vault-backup epic's ship). No
specific feature named — the ask was to find the next real, well-scoped, unblocked gap a prior
epic already flagged, per this session's own established pattern.

## 2. Candidates surveyed, verified against the real code (not just doc claims)

A prior-epic docs sweep (`.pHive/epics/*/docs/*.md`, grepped for "deferred"/"out of
scope"/"follow-up"/"not yet built") surfaced several candidates. Each was checked against the
real code before being treated as real:

- **Session-vault TTL/list gap** and **L2 Pantheon plugin lifecycle wiring** — both already
  shipped (`portunus-session-ttl-and-list`, `portunus-l2-plugin-lifecycle`, both `status:
  shipped`). Not candidates.
- **Full Apple notarization** — blocked on a paid Apple Developer account the user hasn't
  provisioned. Not actionable.
- **A real rotation adapter** (Vercel/GCP/GitHub/Stripe) — blocked on a live external
  account/API to build against; explicitly "deferred until that build actually starts"
  (`portunus-metadata-and-rotation-provenance/docs/design-discussion.md`). Not actionable now.
- **Graph-rendering for `related` links** — explicitly deferred with a stated reason still true
  today (2 real data points in the whole vault, not yet a substrate worth a graph renderer).
  Correctly still low-value.
- **Cross-vault federated search** — explicitly deferred by `portunus-agent-ops-federation` as
  "a real Large-scope epic on its own (needs a 'known vaults' registry, cross-vault ambiguity
  semantics, UI vault-switcher)". Real, unblocked, but speculative: today's actual architecture
  is one shared `PORTUNUS_HOME` vault serving many projects via `project` tags (confirmed by
  this session's own real usage — `demo-cicd`, `demo-project-483920`, `cleanup-tools`,
  `coinfinder`, all in the same vault), with `--home <path>` as an explicit per-invocation escape
  hatch for a genuinely separate vault. No evidence surfaced that the user runs multiple
  separate vaults that would need federating. Presented to the user as an option; not chosen.
- **Bindings/settings UI completeness** — presented to the user as an option; **chosen**. See §3
  for what's real vs. what a first-pass fork report got wrong (corrected by direct inspection).

## 3. The real gap, verified directly (corrects an initial overstatement)

A first pass characterized this as "no settings UI at all — CLI-only." Reading the actual code
(`ui/app/components/ProjectExplorer.tsx`, `ui/app/api/bindings/route.ts`,
`ui/app/components/DetailDrawer.tsx`, `ui/app/api/rotation-status/route.ts`) shows that's only
partially true:

- **Already real, already shipped**: per-project `backend`/`sync_mode` ARE editable today, live,
  in Project Explorer's binding panel — a two-zone real/stub backend picker plus a sync-mode
  select, both calling `POST /api/bindings`. This is not a gap.
- **Real gap #1 — vault-binding `account`/`wif_audience` have no UI.** `VaultBinding` has four
  fields (`backend`, `sync_mode`, `account`, `wif_audience`); the UI panel only edits the first
  two. `POST /api/bindings` (`ui/app/api/bindings/route.ts`) only forwards `backend`/`sync_mode`
  to `portunus bindings set` — `account`/`wif_audience` are CLI-only today, even though `GET
  /api/bindings` already returns their real values.
- **Real gap #2 — rotation bindings have zero write path anywhere in the UI.**
  `/api/rotation-status` is GET-only, feeding `DetailDrawer`'s Auto-rotate button's disabled
  state/tooltip (`rotationStatus?.account || "-"`) — read-only. `portunus rotation-bindings set
  <provider> --account ...` exists CLI-side (the free-text context hint, e.g. a Vercel team
  slug) with no UI equivalent at all.
- **Small, real, unrelated find while reading `ui/app/api/bindings/route.ts`**: its own comment
  says "The WIF audience value itself is never returned by `bindings show`" — verified false by
  direct CLI test (`portunus bindings show <project> --json` returns the real `wif_audience`
  value; confirmed live). Not a security bug (`wif_audience` is documented elsewhere as
  infrastructure topology, not a credential — `backend.py`'s own `VaultBinding` docstring), but
  a stale, incorrect comment worth fixing while this file is already being touched.

## 4. Why this is well-scoped

Both real gaps extend existing, working machinery (`POST /api/bindings`'s pattern, the
`rotation-status` route's GET-only shape) rather than inventing a new page/nav concept — no new
top-level UI surface, no new navigation, no new backend command. Small-to-medium, no external
dependency, directly closes a CLI-vs-UI parity gap now that a real desktop app exists (the
motivating premise of `portunus-desktop-app` was making the vault usable without a terminal —
account/WIF-audience/rotation-account configuration falling back to CLI-only undercuts that).

## 5. Scope note: rotation binding `status` stays code-driven, not UI-editable

`RotationBinding.status` (`"stub"` | `"real"`) reflects whether a real adapter exists in code for
that provider — every provider is a stub today. Letting a UI control flip `status=real` without
a real adapter existing would create a false "this actually rotates" claim with no backing
behavior — the same class of risk `ProjectExplorer`'s own two-zone real/stub backend picker was
built to prevent (a stub that *looks* selectable/configured is a safety bug for a secrets
manager, not a cosmetic gap). The UI addition in scope here only ever sets `account` (the
free-text hint); `status` stays derived from the real adapter registry, unreachable from the UI.
