"""portunus crawl -- discovery/context-bundling for references missing
metadata (portunus-metadata-crawl). A DISCOVERY tool, not a writer: bundles
everything already known about an incomplete reference (sm_name, group,
project, org, repo, its project's VaultBinding, its provider's
RotationBinding) so an LLM (Claude Code, another MCP-connected agent, or a
human) can read it and call the already-shipped
Registry.suggest_metadata()/portunus_suggest_metadata against whichever
fields it has a real proposal for. This module never calls an LLM and never
writes a Reference field itself -- retag() stays the only path that ever
does, exactly as before this module existed.

Real vault data (checked during planning, 393 references) showed `repo` is
set on fewer than 1% of references -- a repo-cloning crawler would have
almost nothing to scan. `sm_name` (often the literal env var name, e.g.
GOOGLE_CLIENT_SECRET) and `group` (91% filled, encodes project/app/env
structure) are the strongest signals actually available today; both are
bundled here. Real external-repo scanning is deliberately out of scope
until repo fill-rate rises (design-discussion.md §2, §6).
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
