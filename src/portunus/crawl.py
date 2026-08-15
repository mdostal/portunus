"""portunus crawl / portunus report (portunus-metadata-crawl).

`crawl_candidates()` is a DISCOVERY tool, not a writer: bundles everything
already known about an incomplete reference (sm_name, group, project, org,
repo, its project's VaultBinding, its provider's RotationBinding) so an LLM
(Claude Code, another MCP-connected agent, or a human) can read it and call
the already-shipped Registry.suggest_metadata()/portunus_suggest_metadata
against whichever fields it has a real proposal for. This module never
calls an LLM and never writes a Reference field itself -- retag() stays the
only path that ever does, exactly as before this module existed.

Real vault data (checked during planning, 393 references) showed `repo` is
set on fewer than 1% of references -- a repo-cloning crawler would have
almost nothing to scan. `sm_name` (often the literal env var name, e.g.
GOOGLE_CLIENT_SECRET) and `group` (91% filled, encodes project/app/env
structure) are the strongest signals actually available today; both are
bundled here. Real external-repo scanning is deliberately out of scope
until repo fill-rate rises (design-discussion.md §2, §6).

`generate_report()` renders current vault state as Markdown -- a real
"deploy docs" starting point (the user's own framing), independent of
whether crawl_candidates() ever found or fixed anything.
"""
from __future__ import annotations

from typing import Dict, List, Optional

from .backend import load_vault_bindings
from .registry import Registry
from .rotation import load_rotation_bindings

# Mirrors the "missing metadata" notion the UI's completeness.ts already
# established (portunus-vault-trust-and-access) -- a reference is a crawl
# candidate if any of these three are unset. Kept in sync by convention,
# not shared code, since one lives in Python (this module, CLI/MCP) and the
# other in TypeScript (the UI) -- same discipline `_build_tree()`'s own
# parallel-not-shared-code precedent with ProjectExplorer already uses.
def _is_incomplete(ref) -> bool:
    return not (ref.description and ref.purpose and (ref.org or ref.project or ref.tags))


def crawl_candidates(
    registry: Registry, org: str = "", project: str = "",
) -> List[Dict]:
    """Return a context bundle for every reference missing metadata,
    optionally scoped by org/project. Read-only, metadata-only -- never a
    value, never a mutation."""
    vault_bindings = load_vault_bindings()
    rotation_bindings = load_rotation_bindings()

    candidates: List[Dict] = []
    for ref in registry:
        if org and ref.org != org:
            continue
        if project and ref.project != project:
            continue
        if not _is_incomplete(ref):
            continue

        binding = vault_bindings.get(ref.project)
        rotation = rotation_bindings.get(ref.provider) if ref.provider else None

        candidates.append({
            "name": ref.name,
            "sm_name": ref.sm_name,
            "group": ref.group,
            "project": ref.project,
            "org": ref.org,
            "repo": ref.repo,
            "source_files": ref.source_files,
            "provider": ref.provider,
            "env": ref.env,
            "missing": {
                "description": not ref.description,
                "purpose": not ref.purpose,
                "org_or_project_or_tags": not (ref.org or ref.project or ref.tags),
            },
            "vault_binding": (
                {
                    "backend": binding.backend, "sync_mode": binding.sync_mode,
                    "account": binding.account, "wif_audience": binding.wif_audience,
                }
                if binding else None
            ),
            "rotation_binding": (
                {"status": rotation.status, "account": rotation.account}
                if rotation else None
            ),
        })
    return candidates


_NO_ORG = "(no org set)"
_NO_PROJECT = "(no project set)"


def generate_report(registry: Registry, org: str = "", project: str = "") -> str:
    """Render current vault state as Markdown -- org -> project structure,
    each reference's known metadata, and an explicit gap section. Read-only,
    metadata-only -- never a value. Useful immediately, with or without any
    crawl-sourced metadata."""
    refs = [
        ref for ref in registry
        if (not org or ref.org == org) and (not project or ref.project == project)
    ]

    by_org: Dict[str, Dict[str, List]] = {}
    for ref in refs:
        o = ref.org or _NO_ORG
        p = ref.project or _NO_PROJECT
        by_org.setdefault(o, {}).setdefault(p, []).append(ref)

    lines = ["# Portunus Vault Report", ""]
    lines.append(f"{len(refs)} reference(s) across {len(by_org)} org(s).")
    lines.append("")

    for o in sorted(by_org):
        lines.append(f"## {o}")
        lines.append("")
        for p in sorted(by_org[o]):
            lines.append(f"### {p}")
            lines.append("")
            for ref in sorted(by_org[o][p], key=lambda r: r.name):
                lines.append(f"- **{ref.name}** (sm_name: `{ref.sm_name}`"
                              + (f", provider: {ref.provider}" if ref.provider else "") + ")")
                if ref.description:
                    lines.append(f"  - description: {ref.description}")
                if ref.purpose:
                    lines.append(f"  - purpose: {ref.purpose}")
                if ref.repo:
                    lines.append(f"  - repo: {ref.repo}")
            lines.append("")

    gaps = [ref for ref in refs if _is_incomplete(ref)]
    lines.append("## Gaps")
    lines.append("")
    if not gaps:
        lines.append("None -- every reference in scope has description/purpose/org set.")
    else:
        for ref in sorted(gaps, key=lambda r: r.name):
            missing = []
            if not ref.description:
                missing.append("description")
            if not ref.purpose:
                missing.append("purpose")
            if not (ref.org or ref.project or ref.tags):
                missing.append("org/project/tags")
            lines.append(f"- **{ref.name}**: missing {', '.join(missing)}")
    lines.append("")

    return "\n".join(lines)
