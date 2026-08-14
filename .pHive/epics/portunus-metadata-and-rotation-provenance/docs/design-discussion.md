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
  posture, applied to a second, genuinely different kind of provider integration. **Confirmed by
  the user: Vercel is the named priority target for the first real adapter** — but not built
  real in this epic ("not going to care too much for first version"); `VercelRotationAdapter`
  ships as a stub too, just the one explicitly earmarked rather than an arbitrary example.

### 3.1.1 Rotation needs its own admin credential — which Portunus vaults *itself*, recursively

Confirmed directly by the user: performing a real rotation means Portunus calling a provider's
own admin/management API (e.g. a Vercel account token with token-management scope) — which is
itself a credential that needs to live somewhere. The user's explicit direction: **that
credential is itself a Portunus-managed reference, resolved through Portunus's own existing
boundary mechanism** — never hardcoded into a `RotationAdapter`, never handled outside the
normal `resolve_call`/`resolve_exec` boundary sinks. Concretely, once a real adapter exists:

```python
class VercelRotationAdapter:
    def rotate(self, ref: Reference, resolver: Resolver) -> None:
        # The admin token itself is just another Reference -- resolved the
        # same boundary-only way every other value in this codebase is,
        # never stored in this adapter's own state, never returned.
        resolver.resolve_call(
            "{{secret:portunus-admin-vercel-token}}",
            boundary=lambda admin_token: _call_vercel_rotate_api(admin_token, ref),
        )
```

This is the same invariant the whole codebase already enforces, applied recursively: Portunus
rotating *other* secrets by using *its own* Portunus-managed admin secret, never a special-cased
credential path outside the normal resolver. No new mechanism needed — `Resolver.resolve_call`
already supports exactly this. Worth naming explicitly in `docs/architecture.md` once a real
adapter exists, since it's a genuinely elegant property of the design, not an accident.

### 3.2 What actually wires to the "Auto-rotate…" button

The button becomes real (clickable, not disabled) only once **both** are true for a given
reference: (a) its `provider` has a `RotationBinding` configured, and (b) that provider's
`RotationAdapter` is real, not a stub. Until then it stays exactly as portunus-swappable-trio
shipped it — disabled, inert, a tooltip explaining why. This is a **UI state derived from real
backend state**, not a separate flag to keep in sync — the button's enabled-ness is computed
from whether a real adapter exists for that reference's provider, the same way ARCA's two-zone
picker already derives real-vs-stub from the backend registry rather than a separately
maintained list.

### 3.3 Resolved by the user, and what's still open

**Resolved:** Vercel is the confirmed priority provider (§3.1's `VercelRotationAdapter`, still
shipping as a stub this epic). The recursive self-vaulted-admin-credential pattern (§3.1.1) is
confirmed as the design — no separate credential-handling path for rotation, ever.

**Still open, deliberately not decided here:**
- Audit/Petitio implications of an automated rotation actually firing (should rotating a
  credential require the same approval gate injecting it does? reasonable default: yes, but not
  decided here — moot until a real adapter exists to fire anything).
- Whether `RotationBinding` needs anything beyond a free-text account/context hint once a real
  adapter is actually being built (e.g. does a Vercel rotation need a team ID *and* a project
  ID, not just one context string?) — deferred until that build actually starts.

### 3.4 Confirmed already shipped: bulk plaintext import

The user's stated need — "drop plaintext passwords... vaulted local... enable the coin finder
to try a bunch of passwords locally... another free password manager" — is **already shipped**,
not new scope: `portunus_drop_bulk` (MCP) / `portunus drop-bulk` (CLI), from
portunus-vault-routing (v0.13.0), does exactly this — many local secrets in one call, each
independently encrypted at rest, each resolvable later via the same boundary-only mechanism
(`portunus_resolve_exec` for "try this one against something without me ever seeing which
worked"). No new work needed for the mechanical bulk-import piece.

**A genuinely new, not-yet-built direction the user's framing surfaces**: "another free
password manager THEY can unlock" implies a *human* directly retrieving their own vaulted
value — not an LLM/agent resolving it at a boundary. Nothing in Portunus does this today; every
existing resolve path (CLI, UI, MCP) is boundary-only by design, even for a human operator
(`portunus resolve` writes a tempfile, never prints the value). A deliberate, explicit,
human-only "reveal" action would be a real, different capability — worth naming here so it
isn't lost, explicitly **not scoped into this epic** (this epic is metadata + rotation
provenance; a human-reveal UX is its own product decision with its own UX/security
considerations — e.g. should it require re-authentication, should it be logged differently from
an LLM-facing resolve).

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

**Confirmed by the user**: Vercel as priority target, the recursive self-vaulted-admin-credential
rotation pattern, bulk plaintext import already shipped. Proceeding to story decomposition.
