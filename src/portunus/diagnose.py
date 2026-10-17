"""Reverse rotation diagnosis: "this stopped working around time X -- what
changed near then?" (GitHub #208).

Runs the audit log backwards from a symptom: take a time window, keep the
events that change what a credential *is* (rotations, re-stores, removals,
imports -- not the resolves that merely read it), and rank the references
they touched by proximity to the project that broke. Metadata only: audit
entries never carry a value, and neither does anything here.

Proximity tiers, nearest first:

- ``project`` -- the reference's own project, repo or group root is the
  symptom's project.
- ``related`` -- linked to or from a ``project``-tier reference through
  ``related``, or sharing its group root.
- ``elsewhere`` -- everything else that changed in the window, including
  audit names no reference maps to any more.

The full "everywhere it should live" walk belongs to the dependency-graph
work (GitHub #207); until then a candidate's own ``injected_as`` is reported
as where it should be re-injected.
"""
from __future__ import annotations

import re
import time
from datetime import datetime, timezone
from typing import Dict, Iterable, List, Optional, Tuple

# Actions that change a stored credential (or where it lives), as opposed to
# reading or gating it. A prefix match, so every `rotate:<provider>` counts.
CHANGE_ACTION_PREFIXES = (
    "rotate", "drop", "retag", "oauth_store", "oauth_remove",
    "session_store", "session_remove", "leak-mark-rotated",
    "vault_import", "vault_access_import",
)

TIERS = ("project", "related", "elsewhere")

_RELATIVE = re.compile(r"^(\d+)([smhdw])$")
_UNIT_SECONDS = {"s": 1, "m": 60, "h": 3600, "d": 86400, "w": 7 * 86400}


def parse_when(text: str, now: Optional[float] = None, end_of_day: bool = False) -> int:
    """Epoch seconds for a CLI time bound. Accepts a relative age (``90m``,
    ``36h``, ``14d``, ``2w`` -- that long before now), an ISO date
    (``2026-10-15``, UTC midnight, or the end of that day when
    ``end_of_day``), an ISO datetime (naive means UTC), or raw epoch
    seconds. Raises ValueError on anything else."""
    text = (text or "").strip()
    if not text:
        raise ValueError("empty time bound")
    now = time.time() if now is None else now
    m = _RELATIVE.match(text)
    if m:
        return int(now - int(m.group(1)) * _UNIT_SECONDS[m.group(2)])
    if text.isdigit():
        return int(text)
    if re.fullmatch(r"\d{4}-\d{2}-\d{2}", text):
        day = datetime.strptime(text, "%Y-%m-%d").replace(tzinfo=timezone.utc)
        return int(day.timestamp()) + (86400 - 1 if end_of_day else 0)
    try:
        dt = datetime.fromisoformat(text.replace("Z", "+00:00"))
    except ValueError:
        raise ValueError(
            f"unrecognised time {text!r} -- use 2026-10-15, 2026-10-15T09:30, "
            "epoch seconds, or a relative age like 36h / 14d") from None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return int(dt.timestamp())


def format_ts(ts) -> str:
    """Compact UTC rendering of an entry's ``ts`` ("-" when it has none)."""
    if not isinstance(ts, int) or isinstance(ts, bool):
        return "-"
    return datetime.fromtimestamp(ts, tz=timezone.utc).strftime("%Y-%m-%dT%H:%MZ")


def is_change(entry: dict) -> bool:
    return str(entry.get("action", "")).startswith(CHANGE_ACTION_PREFIXES)


def _ts(entry: dict) -> Optional[int]:
    ts = entry.get("ts")
    return ts if isinstance(ts, int) and not isinstance(ts, bool) else None


def in_window(entries: Iterable[dict], since: Optional[int], until: Optional[int]
              ) -> Tuple[List[dict], int]:
    """(entries whose ``ts`` falls in [since, until], count of entries that
    have no ``ts`` and so cannot be placed in any window). With neither
    bound set every entry passes and the untimed count is 0."""
    if since is None and until is None:
        return list(entries), 0
    kept, untimed = [], 0
    for e in entries:
        ts = _ts(e)
        if ts is None:
            untimed += 1
            continue
        if since is not None and ts < since:
            continue
        if until is not None and ts > until:
            continue
        kept.append(e)
    return kept, untimed


def _by_audit_name(registry) -> Dict[str, object]:
    """Audit ``secret`` field -> reference. Most writers log the SM name,
    a few log the reference name, so both map."""
    out: Dict[str, object] = {}
    for ref in registry:
        out.setdefault(ref.name, ref)
        if ref.sm_name:
            out[ref.sm_name] = ref
    return out


def _group_root(ref) -> str:
    return (getattr(ref, "group", "") or "").strip("/").split("/", 1)[0]


def in_project(ref, project: str) -> bool:
    return bool(project) and project in (ref.project, ref.repo, _group_root(ref))


def _tiers(refs: List, project: str) -> Dict[str, str]:
    """Reference name -> proximity tier for every reference in `refs`."""
    if not project:
        return {r.name: "elsewhere" for r in refs}
    near = {r.name for r in refs if in_project(r, project)}
    near_roots = {_group_root(r) for r in refs if r.name in near} - {""}
    linked = set()
    for r in refs:
        if r.name in near:
            linked.update(r.related or [])
        elif set(r.related or []) & near:
            linked.add(r.name)
    out = {}
    for r in refs:
        if r.name in near:
            out[r.name] = "project"
        elif r.name in linked or (_group_root(r) and _group_root(r) in near_roots):
            out[r.name] = "related"
        else:
            out[r.name] = "elsewhere"
    return out


def diagnose(entries: List[dict], registry, since: Optional[int], until: Optional[int],
             project: str = "", include_all: bool = False) -> dict:
    """Group the window's change events by reference and rank them by
    proximity to `project`, then by how many changes landed, then by most
    recent. ``include_all`` keeps non-change events (resolves, gates, ...)
    as well. Returns a JSON-safe dict; never a value."""
    windowed, untimed = in_window(entries, since, until)
    events = windowed if include_all else [e for e in windowed if is_change(e)]

    by_name = _by_audit_name(registry)
    refs = list(registry)
    tiers = _tiers(refs, project)

    buckets: Dict[str, dict] = {}
    for e in events:
        audit_name = str(e.get("secret", ""))
        ref = by_name.get(audit_name)
        key = ref.name if ref is not None else audit_name
        b = buckets.get(key)
        if b is None:
            b = buckets[key] = {
                "reference": ref.name if ref is not None else None,
                "audit_name": audit_name,
                "tier": tiers.get(ref.name, "elsewhere") if ref is not None else "elsewhere",
                "project": ref.project if ref is not None else "",
                "provider": ref.provider if ref is not None else "",
                "group": ref.group if ref is not None else "",
                "injected_as": dict(ref.injected_as) if ref is not None else {},
                "events": [],
            }
        b["events"].append({k: e.get(k) for k in ("seq", "ts", "actor", "action", "result")})

    def rank(b: dict):
        latest = max((ev["ts"] or 0) for ev in b["events"])
        return (TIERS.index(b["tier"]), -len(b["events"]), -latest, b["audit_name"])

    candidates = sorted(buckets.values(), key=rank)
    return {
        "since": since, "until": until, "project": project,
        "untimed_entries": untimed,
        "candidates": candidates,
    }
