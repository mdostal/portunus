# Grill Record — portunus-session-ttl-and-list

**Source draft:** .pHive/epics/portunus-session-ttl-and-list/docs/design-discussion.md
**CONTEXT.md substrate:** present
**inconsistency_risk_signals:** absent (heuristic pass)
**round_number:** 1
**unresolved_count:** 1
**Generated:** 2026-08-13T00:00:00Z

## Summary

- Vocabulary mismatches: 1 finding
- Hidden assumptions: clean (verified: grep confirms no other caller of load_session/
  inspect_session/store_session/remove_session exists outside localvault.py/test_localvault.py,
  so the behavior change is fully contained)
- Unresolved tensions: clean
- Convention violations: clean
- Posture mismatches: clean

## Vocabulary mismatches

- **V1** — `.pHive/CONTEXT.md`'s Terminology section has no entry at all for session storage
  (`store_session`/`SESSION_SCHEMA`/the `session:<site>:<account>` namespace), even though this
  capability already shipped via PAN-7831 before this epic.
  - Draft location: whole document (not mentioned)
  - Reference: `.pHive/CONTEXT.md` Terminology section
  - Question for planner: fold a CONTEXT.md update into this epic's closeout, covering both the
    pre-existing session-storage vocabulary (a pre-existing gap, not introduced by this epic)
    and the new `SessionExpired`/`list_sessions()` vocabulary this epic adds.

## Notes

Small, well-contained fix. Resolving V1 now: yes, fold into story 2.

## Out of scope (this pass)
