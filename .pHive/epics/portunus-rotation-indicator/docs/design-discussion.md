# Design Discussion: portunus-rotation-indicator

## 1. What Are We Doing?

Closing the loop `portunus-agent-ops-federation` (v0.3.0) opened but didn't finish: an agent's
`portunus ask "rotate ..."` request sets `tags.rotation_requested = "true"` on the reference
(via `Registry.retag()`), but nothing in the UI ever shows that flag. A human has no way to see
"this needs rotating" without running `portunus find` by hand. Pure UI display change — no
backend/CLI changes needed.

## 2. What I Found

`PortunusReference.tags` is already `Record<string, string>` on the frontend (`ui/app/types.ts`)
and the registry API already returns the full tags dict (`/api/registry` -> `portunus reg
json`). `Console.tsx`'s table row, `VaultMap.tsx`'s card, and `DetailDrawer.tsx` all render
`reference`/`r` directly and already have a `tags-row`/chip rendering convention (`StatePill`,
`.chip`) to extend.

Confirmed via re-reading `AddSecretForm`'s rotate-prefill path (`page.tsx`'s `rotateDraft`):
rotating via the existing "Rotate…" button (which opens the add-secret form pre-filled with
name/sm_name/provider/project/env, then re-`drop`s) already clears `rotation_requested` as a
side effect, since `Registry.add()` replaces the whole `Reference` record rather than merging
tags. So the flag is already self-clearing once a human acts on it — this epic only needs to
make it *visible* before that point.

## 3. My Proposed Approach

A small `RotationBadge` component (`⟳ rotation requested`, using the existing `--warn` token —
distinct from `--request`, which is already the `requested`-state color, so the two visual
signals ["this reference IS a value-less placeholder" vs "this reference NEEDS rotating"]
don't collide) rendered whenever `reference.tags.rotation_requested === "true"`:

- **Console** — small badge next to the state pill in the table row.
- **Vault Map** — small badge on the card.
- **DetailDrawer** — a clear banner-style note near the top (this is where the human acts, so
  it should be the most prominent).

## 4. What Could Go Wrong

- **[low] The badge never clears because the human doesn't use the Rotate button (edits tags
  another way).** Acceptable — matches the design discussion's `M` risk framing from the prior
  epic already ("requested references accumulate as clutter" was already an accepted known gap
  for the `requested` *state*; the same acceptance applies to a stale `rotation_requested` tag).
  Not a new risk this epic introduces.

## 5. Dependencies and Constraints

None beyond what already shipped. Pure frontend change, three files.

## 6. Open Questions

None.

## 7. Verification Strategy

```
VERIFICATION PLAN:
  Tools: npm run build (TypeScript), manual smoke test
  Automated: none new (no backend/API change to unit test) -- `npm run build` is the only gate
  Manual: verify the badge appears/disappears correctly against a live registry with a
    rotation_requested-flagged reference
  Not verifying: automated UI component tests (no UI test framework exists yet, same gap noted
    in portunus-standalone-core's structured outline)
```

## 8. Scale Assessment

```
SCALE ASSESSMENT:
  Files affected: 4 (new RotationBadge component + 3 call sites)
  Subsystems: UI only
  Migration required: no
  Unknowns: 0

  RECOMMENDATION: Proceed directly to stories (Small scope)
  RATIONALE: Pure display change over already-shipped data; no new API surface.
```
