# Grill Record — portunus-gcp-multi-account

**Source draft:** .pHive/epics/portunus-gcp-multi-account/docs/design-discussion.md
**CONTEXT.md substrate:** present
**inconsistency_risk_signals:** absent (heuristic pass)
**round_number:** 1
**unresolved_count:** 1
**Generated:** 2026-08-13T20:45:00Z

## Summary

- Vocabulary mismatches: clean
- Hidden assumptions: clean (the `--account=` mechanism was empirically verified live this
  session, not just assumed -- `gcloud secrets list --account=mdostal@ff.events
  --project=ffe-cicd` already ran successfully during research)
- Unresolved tensions: 1 finding
- Convention violations: clean
- Posture mismatches: clean

## Unresolved tensions

- **U1** — §3 Slice D says `portunus bindings show` prints "metadata only... the WIF audience
  string is topology, already accepted as displayable," but the UI epic
  (portunus-vault-metadata-ui) established a stricter rule for the *browser* surface: the
  audience is boolean-presence-only there (`wif_configured`), never the literal string. The
  draft doesn't explicitly reconcile why the CLI is allowed to show more than the UI does.
  - Draft location: §3 Slice D
  - Tension: CLI shows the real audience string vs. UI shows presence-only.
  - Question for planner: resolving now — this is a deliberate, different bar for a different
    surface, not an inconsistency: the UI rule was specifically about not exposing
    infrastructure topology over a web-rendered surface (screen-sharing/accidental-exposure
    risk, per portunus-vault-metadata's grill H1 reasoning). `portunus bindings show` is a
    local CLI the operator runs against their own machine to inspect their own config, same
    trust boundary as reading `gcp-bindings.json` directly with `cat` — showing the real value
    there is not a new exposure, it's the same information already sitting in a 0600 file the
    operator already has read access to. State this explicitly in the story's acceptance
    criteria so the CLI/UI difference reads as intentional, not overlooked.

## Notes

Small, well-verified draft -- the fix itself was proven to work during research (not just
designed on paper), which is the strongest possible grounding for a "proceed directly to
stories" recommendation.

## Out of scope (this pass)

Grill does NOT propose solutions, score quality, gate work, or prioritize findings.
