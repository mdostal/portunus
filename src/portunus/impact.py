"""Blast radius of a reference -- "if I rotate this, what breaks?" (PANT-1122).

A read-only traversal over metadata the registry already holds: `related`
(walked transitively, in BOTH directions -- a reference that lists this one
as related is just as touched as one this one lists), plus every reached
reference's `project`, `env`, `group`, `repo`, `source_files` and
`injected_as` targets. No new schema, no graph store: the flat fields are
the graph. Never touches a backend, never reads a value.

The auto-suggested layer (`suggest_related`) is deliberately separate from
the traversal and never written anywhere: it proposes references that share
a repo, a source file, or a group parent with the root, labelled with the
reason, so a human can promote them into `related` via `portunus retag
--related`. The human-curated `related` list stays the only thing the
traversal itself follows -- a suggestion never silently becomes an edge.
"""
from __future__ import annotations

from collections import deque
from typing import Dict, List, Optional

from .registry import Reference, Registry

# Suggestions are a hint list, not a report -- one shared repo can hold
# dozens of references, and past this many the list stops being readable.
MAX_SUGGESTIONS = 20


def _node(ref: Reference, depth: int, via: Optional[str], direction: str) -> dict:
    return {
        "name": ref.name,
        "depth": depth,
        "via": via,
        # "root" | "related" (via lists this ref) | "related-by" (this ref lists via)
        "direction": direction,
        "state": ref.state,
        "provider": ref.provider,
        "org": ref.org,
        "project": ref.project,
        "env": ref.env,
        "group": ref.group,
        "repo": ref.repo,
        "source_files": list(ref.source_files),
        "injected_as": dict(ref.injected_as),
    }


def _inbound_index(registry: Registry) -> Dict[str, List[str]]:
    """target name -> names of every reference that lists it in `related`."""
    inbound: Dict[str, List[str]] = {}
    for ref in registry:
        for rel in ref.related:
            inbound.setdefault(rel, []).append(ref.name)
    return inbound


def _group_parent(group: str) -> str:
    segments = [s for s in group.split("/") if s]
    # A one-segment parent ("project-y") is the whole project -- too broad
    # to mean "these rotate together", so only 2+ segment parents count.
    return "/".join(segments[:-1]) if len(segments) >= 3 else ""


def suggest_related(registry: Registry, name: str) -> List[dict]:
    """References that look related to `name` but aren't linked either way.

    Signals, strongest first: a shared source file in the same repo/project,
    the same group, a shared group parent (2+ segments deep), the same repo.
    Read-only -- returns proposals with reasons, never edits `related`."""
    root = registry.require(name)
    linked = set(root.related) | {
        r.name for r in registry if name in r.related
    }
    root_files = {
        (root.repo or root.project, f) for f in root.source_files if root.repo or root.project
    }
    root_parent = _group_parent(root.group)
    scored = []
    for ref in registry:
        if ref.name == name or ref.name in linked:
            continue
        reasons = []
        shared = sorted(
            f for (scope, f) in root_files
            if scope == (ref.repo or ref.project) and f in ref.source_files
        )
        if shared:
            reasons.append(f"shared source file: {', '.join(shared)}")
        if root.group and ref.group == root.group:
            reasons.append(f"same group: {root.group}")
        elif root_parent and _group_parent(ref.group) == root_parent:
            reasons.append(f"shared group parent: {root_parent}")
        if root.repo and ref.repo == root.repo:
            reasons.append(f"same repo: {root.repo}")
        if reasons:
            scored.append({"name": ref.name, "reasons": reasons})
    scored.sort(key=lambda s: (-len(s["reasons"]), s["name"]))
    return scored[:MAX_SUGGESTIONS]


def impact(
    registry: Registry, name: str, *, max_depth: Optional[int] = None, suggest: bool = True,
) -> dict:
    """Walk `related` transitively from `name` and collect everything the
    reached references are wired into. Raises KeyError for an unknown name.

    `max_depth=None` walks the whole connected component (cycle-safe);
    `max_depth=1` is direct links only. A `related` name missing from the
    registry is reported under `unresolved`, never an error."""
    root = registry.require(name)
    inbound = _inbound_index(registry)
    nodes = [_node(root, 0, None, "root")]
    edges: List[dict] = []
    unresolved: List[dict] = []
    seen = {root.name}
    queue = deque([(root, 0)])
    while queue:
        ref, depth = queue.popleft()
        if max_depth is not None and depth >= max_depth:
            continue
        neighbours = [(rel, "related") for rel in ref.related]
        neighbours += [(src, "related-by") for src in sorted(inbound.get(ref.name, []))]
        for other_name, direction in neighbours:
            other = registry.get(other_name)
            if other is None:
                unresolved.append({"from": ref.name, "name": other_name})
                continue
            edges.append({"from": ref.name, "to": other_name, "direction": direction})
            if other_name in seen:
                continue
            seen.add(other_name)
            nodes.append(_node(other, depth + 1, ref.name, direction))
            queue.append((other, depth + 1))

    def _collect(key):
        return sorted({n[key] for n in nodes if n[key]})

    files = sorted({
        f"{n['repo']}:{f}" if n["repo"] else f
        for n in nodes for f in n["source_files"]
    })
    targets = sorted({
        f"{n['name']} {env}={target}"
        for n in nodes for env, target in n["injected_as"].items()
    })
    return {
        "root": root.name,
        "nodes": nodes,
        "edges": edges,
        "unresolved": unresolved,
        "summary": {
            "other_references": len(nodes) - 1,
            "projects": _collect("project"),
            "repos": _collect("repo"),
            "envs": _collect("env"),
            "groups": _collect("group"),
            "files": files,
            "injection_targets": targets,
        },
        "suggested_related": suggest_related(registry, name) if suggest else [],
    }


def summary_line(result: dict) -> str:
    """One line for a pre-rotation preview: "touches N other references in M projects"."""
    s = result["summary"]
    n, m = s["other_references"], len(s["projects"])
    return (
        f"rotating {result['root']} touches {n} other reference{'' if n == 1 else 's'} "
        f"in {m} project{'' if m == 1 else 's'}"
        + (f" ({', '.join(s['projects'])})" if s["projects"] else "")
    )


def _node_detail(n: dict) -> str:
    parts = [f"{k}={n[k]}" for k in ("project", "env", "repo", "group", "state") if n[k]]
    return "  ".join(parts)


def render_text(result: dict) -> str:
    """The traversal as an indented tree, then the flattened blast radius."""
    by_name = {n["name"]: n for n in result["nodes"]}
    children: Dict[str, List[dict]] = {}
    for n in result["nodes"][1:]:
        children.setdefault(n["via"], []).append(n)

    lines = [summary_line(result), ""]

    def walk(n: dict, indent: int) -> None:
        pad = "  " * indent
        label = n["name"] if n["direction"] == "root" else (
            f"{n['name']}  ({'related' if n['direction'] == 'related' else 'lists ' + n['via'] + ' as related'})"
        )
        lines.append(f"{pad}{label}")
        detail = _node_detail(n)
        if detail:
            lines.append(f"{pad}    {detail}")
        for env, target in sorted(n["injected_as"].items()):
            lines.append(f"{pad}    injected_as {env}={target}")
        for f in n["source_files"]:
            lines.append(f"{pad}    file {n['repo'] + ':' if n['repo'] else ''}{f}")
        for child in children.get(n["name"], []):
            walk(child, indent + 1)

    walk(by_name[result["root"]], 0)

    s = result["summary"]
    lines.append("")
    for label, key in (("projects", "projects"), ("repos", "repos"), ("envs", "envs"), ("files", "files")):
        lines.append(f"{label}: {', '.join(s[key]) if s[key] else '-'}")
    if s["injection_targets"]:
        lines.append("injection targets:")
        lines.extend(f"  {t}" for t in s["injection_targets"])
    if result["unresolved"]:
        lines.append("unresolved related (not in registry): " + ", ".join(
            f"{u['name']} (from {u['from']})" for u in result["unresolved"]
        ))
    if result["suggested_related"]:
        lines.append("")
        lines.append(
            "suggested related -- NOT linked, not walked above; promote with "
            f"`portunus retag {result['root']} --related ...` (replaces the list):"
        )
        for sug in result["suggested_related"]:
            lines.append(f"  {sug['name']}  [{'; '.join(sug['reasons'])}]")
    return "\n".join(lines)
