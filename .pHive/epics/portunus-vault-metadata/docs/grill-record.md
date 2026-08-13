# Grill Record — portunus-vault-metadata

**Source draft:** .pHive/epics/portunus-vault-metadata/docs/design-discussion.md
**CONTEXT.md substrate:** present
**inconsistency_risk_signals:** absent (heuristic pass)
**round_number:** 1
**unresolved_count:** 3
**Generated:** 2026-08-13T00:00:00Z

## Summary

- Vocabulary mismatches: 1 finding
- Hidden assumptions: 1 finding
- Unresolved tensions: 1 finding
- Convention violations: clean
- Posture mismatches: clean

## Vocabulary mismatches

- **V1** — §3 Slice E says AWS becomes "a legal `Reference.provider` value," implying some kind
  of enforced enumeration, but `registry.py` has no `VALID_PROVIDERS`/enum anywhere — `provider`
  is (and always has been) a free string, same as `kind`/`scope`. "Legal" overstates what
  actually changes.
  - Draft location: §3 Slice E
  - Question for planner: correct the wording — nothing needs to enforce `provider="aws"` as a
    value; the only real change is that a backend now *exists* for it and fails clearly instead
    of the current situation (an unrecognized provider silently routing to whatever `_build()`
    happens to construct, i.e. `GcloudBackend`, and failing with a confusing GCP-flavored
    error). State that failure mode explicitly as the actual "before" state being fixed.

## Hidden assumptions

- **H1** — §3 Slice B assumes a new `gcp-bindings.json` file living "alongside `registry.json`"
  is safe with no stated file permissions, but it can carry WIF audience strings — full resource
  names like `//iam.googleapis.com/projects/<num>/locations/global/workloadIdentityPools/<pool>/
  providers/<provider>` — which are infrastructure topology, not values, but still more sensitive
  than "purely informational" implies (project numbers, pool/provider identifiers useful for
  reconnaissance if a repo/vault is ever exposed).
  - Draft location: §3 Slice B, §6 Open Question 1
  - Question for planner: resolving now — `gcp-bindings.json` gets the same `0600` treatment
    `Registry`'s docstring already promises for `registry.json` (mirror the existing file-perm
    convention rather than introduce a new one). Not secret-grade encryption, just matching the
    existing "not world-readable" bar the rest of `PORTUNUS_HOME` already holds itself to.

## Unresolved tensions

- **U1** — §3 Slice C's `--register` flow says a discovered secret becomes a new `Reference`
  with `sm_name` set from the discovered name, but never says what the local `name` (the
  registry's actual dict key, distinct from `sm_name`) becomes. Two different GCP projects can
  legally have same-named secrets (e.g. both `personalsites-487021` and `firefly-events-inc`
  could have an `API_KEY`) — using the bare discovered name as `name` risks a second discovery
  run silently colliding with/overwriting an unrelated reference from a different project.
  - Draft location: §3 Slice C
  - Tension: "register instead of re-created blind" (the whole point of discovery) vs. no
    stated collision-avoidance rule for the local key itself.
  - Question for planner: resolving now — derive `name` as `<project>-<discovered-sm-name>`
    (lowercased, matching the existing `dostal-shared-*`/`demo-*` naming convention already
    visible in the registry), and if that derived name already exists as a *different*
    `sm_name`/`project` pair, skip that entry and report it in `discover`'s output as a
    naming conflict rather than silently overwriting — never blind-overwrite an existing
    reference regardless of state.

## Posture mismatches

No findings.

## Notes

Six slices, mostly independent (A gates D; B gates C; E and F are standalone) — sequencing
matches the design discussion's own dependency section, no new ordering constraint surfaced
here. The two real fixes (H1 file perms, U1 naming collision) are both small, additive
clarifications, not scope changes.

## Out of scope (this pass)

Grill does NOT propose solutions, score quality, gate work, or prioritize findings.
