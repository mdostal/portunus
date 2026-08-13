# Research Brief: portunus-agent-ops-federation

## Requirement

Deepen `portunus-standalone-core` (shipped v0.2.0) along three gaps identified in that epic's
own traceability check:

1. Agent-initiated add/roll via `portunus ask` — today `ask` is fetch/inject-only.
2. A "move" action in the UI — relocate/re-tag a reference between scope/project/env.
3. Cross-repo/multi-vault support — `PORTUNUS_HOME` is single-vault per process today; the
   north star says "across all repos."

## Current state (verified against code)

- `paths.py::home()` resolves `PORTUNUS_HOME` (or `DOSTAL_SECRETS_HOME`) once per process from
  the environment. No per-invocation override, no concept of multiple vaults.
- `Registry` has `add`/`remove`/`set_state`/`set_approval`/`resolve_by_tags`/
  `migrate_legacy_tags` — no method to update an existing reference's tags/provider/project/env
  in place (a "move"/re-tag would require this).
- `intent.py::parse_intent()` only ever produces a tag dict for *lookup* — there's no concept
  of an "intent kind" (fetch vs. add vs. rotate). `cli.py::cmd_ask` always resolves via
  `resolve_by_tags` and dispatches to an adapter; it has no path for "this reference doesn't
  exist yet, create a placeholder" or "flag this reference for rotation."
- `Registry.VALID_STATES = ("enabled", "locked", "dropped", "revoked")` — no "requested" or
  "rotation-pending" state exists yet.
- The non-negotiable invariant (README, CONTEXT.md) is that an agent must never see a secret
  value. Any "agent-initiated add/roll" design has to route the actual value-providing act
  through the existing harness-side-only `drop` path — an agent can *request*, never *supply*.
- UI (`ui/app/page.tsx` + components) has view/add/rotate; rotate already falls back to the
  add-secret form (story 06 design decision) rather than a server-side regenerate. No "move"
  action exists in Console, Vault Map, or DetailDrawer.

## Constraints (cross-cutting-concerns.yaml)

`secret-boundary-invariant` and `audit-chain-integrity` apply to every new path here, same as
the prior epic.

## Scope decision for this pass

Cross-repo support scoped down to a `--home <path>` CLI override (explicit, per-invocation
vault selection) rather than automatic multi-vault federated search — the latter is a real
Large-scope epic on its own (needs a "known vaults" registry, cross-vault ambiguity semantics,
UI vault-switcher) and shouldn't be bolted onto this Medium-scope pass.
