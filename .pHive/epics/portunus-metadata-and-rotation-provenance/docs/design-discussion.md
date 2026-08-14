# Design Discussion — portunus-metadata-and-rotation-provenance

## 1. Goal

Two separable pieces, per the research brief's finding:

1. Close the real UI gap: creation-time (`AddSecretForm`) and edit-time (`DetailDrawer`'s Move
   form) currently expose different metadata field sets, and `kind`/`scope`/`backend` aren't
   exposed in the UI at all. Unify them.
2. Give Portunus a structured place to record **rotation provenance** — which provider issued a
   reference and what account/context would be needed to actually rotate it — as the honest,
   ARCA-stub-shaped prerequisite for turning the "Auto-rotate…" button from inert to real, one
   provider at a time, later.

This document does **not** propose building any real rotation integration (Vercel/GCP/GitHub/
Stripe rotation calls). That's provider-specific follow-on work, deliberately out of scope here,
same posture as ARCA's real-vs-stub backend split.

## 2. Slice A — unify metadata across create and edit

`AddSecretDraft` (`ui/app/types.ts`) grows to match `DetailDrawer`'s Move form's field set plus
the CLI/MCP-only fields: `kind`, `scope`, `backend`, `description`, `purpose`, `injected_as`,
`group`, `related`, alongside the existing `name`/`sm_name`/`provider`/`project`/`env`/`tags`.
`AddSecretForm.tsx` gains the missing inputs (matching `DetailDrawer`'s existing input patterns
exactly — same labels, same placeholder style). `/api/drop`'s route already shells out to
`portunus drop`, which already accepts every one of these fields via CLI flags (confirmed
directly against `cli.py`'s argparser: `--scope`, `--kind`, `--description`, `--purpose`,
`--injected-as`, `--group`, `--related` all exist on `drop` today) — this is a pure
UI-completeness change for those, no backend work. **`--backend` does not exist on `cmd_drop`
today** (confirmed by the same check) even though `portunus_drop`'s MCP tool already has a
`backend` parameter (added in portunus-swappable-trio) and `reg add`/`retag` both support it —
this one real, small gap needs a new CLI flag on `drop` before the UI can rely on it end to end.

Low risk, mechanical, no new concepts — this slice alone is worth shipping even if rotation
provenance below turns out to need more design time.

## 3. Slice B — rotation provenance (provenance only, no real rotation)

### 3.1 What it needs to capture

Per the research brief's per-provider survey, actually rotating a credential needs, at minimum:
*which provider*, *what account/API context*, and *whether Portunus even has an integration for
it yet*. This maps cleanly onto the same real/stub posture ARCA already uses:

- **`Reference.provider`** already answers "which provider" — reused as-is, not duplicated. The
  existing free-text tag is sufficient for this; no schema change needed there.
- **New: a `RotationBinding`** (naming TBD — avoid colliding with `VaultBinding`'s established
  shape while deliberately mirroring its pattern): keyed by `provider` value (not by individual
  reference — most references sharing a provider share a rotation account/context, same
  reasoning `VaultBinding` uses per-project rather than per-reference). Fields: `provider`,
  `status` ("real" | "stub" — matching ARCA's own real/stub language), `account`/context hint
  (free-text, provider-specific meaning — e.g. a Vercel team slug, a GitHub org), stored the
  same way `VaultBinding` is (`PORTUNUS_HOME/rotation-bindings.json`, 0600, JSON+flock+atomic
  idiom every other store in this codebase already uses).
- **New: `RotationAdapter` stub registry** — mirrors ARCA's `SecretBackend` split exactly:
  `VercelRotationAdapter`, `GitHubRotationAdapter`, `StripeRotationAdapter`, etc., each a small
  class whose `rotate(ref)` unconditionally raises (matching every ARCA stub's docstring/error-
  message pattern) until a real one gets built. This is the direct analog of ARCA's honest-stub
  posture, applied to a second, genuinely different kind of provider integration.

### 3.2 What actually wires to the "Auto-rotate…" button

The button becomes real (clickable, not disabled) only once **both** are true for a given
reference: (a) its `provider` has a `RotationBinding` configured, and (b) that provider's
`RotationAdapter` is real, not a stub. Until then it stays exactly as portunus-swappable-trio
shipped it — disabled, inert, a tooltip explaining why. This is a **UI state derived from real
backend state**, not a separate flag to keep in sync — the button's enabled-ness is computed
from whether a real adapter exists for that reference's provider, the same way ARCA's two-zone
picker already derives real-vs-stub from the backend registry rather than a separately
maintained list.

### 3.3 Explicitly deferred, not decided here

- Which provider gets the first *real* rotation adapter (Vercel is the research brief's
  best-fit candidate — simple token mint/revoke REST API — but this is a product decision, not
  an engineering one, and isn't made here).
- Whether `RotationBinding` needs anything beyond a free-text account/context hint (real auth
  for a rotation call — e.g. a Vercel account token — is itself a *secret Portunus would need to
  manage*, which raises the question of whether rotation credentials get vaulted in Portunus
  itself, a genuinely recursive design question worth its own discussion once a real adapter is
  actually being built).
- Audit/Petitio implications of an automated rotation actually firing (should rotating a
  credential require the same approval gate injecting it does? reasonable default: yes, but not
  decided here).

## 4. Risks

| Risk | Severity | Mitigation |
|---|---|---|
| `RotationBinding`/`RotationAdapter` naming or shape gets built ahead of a real use case and has to be reworked once the first real adapter lands | Medium | Keep Slice B to provenance/config only — no real rotation logic to rework, just a config schema; explicitly named as provisional in this doc |
| The "Auto-rotate" button's derived-enabled-state logic gets out of sync with the real adapter registry | Low | Same pattern ARCA's two-zone picker already uses successfully — compute from backend state, never a separately maintained flag |
| Scope creep into building a real rotation adapter before the provenance layer is even used by anything | Medium | Explicitly deferred in §3.3; this epic stops at provenance + stubs, mirroring ARCA's own real/stub sequencing discipline |

## 5. Scale assessment

**Slice A: Small.** Pure UI field-completeness, existing backend support, mechanical.
**Slice B: Medium.** New config concept + stub registry, no real integration, no external
dependency — but touches a genuinely new area (rotation, not storage/access) that deserves a
confirmation checkpoint before story decomposition, same discipline used for portunus-vault-
routing and portunus-swappable-trio's own Large/Medium-scope pauses.

Not decomposed into stories yet — presenting for confirmation first, per the user's own "we'll
get there" framing (no urgency) and the genuine open questions in §3.3.
