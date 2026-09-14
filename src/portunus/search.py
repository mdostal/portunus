"""Free-text search over the already-loaded Registry — pure metadata filtering,
never touches a backend or returns a value.
"""
from __future__ import annotations

from typing import List, Optional

from .registry import Registry, Reference


def _matches_query(ref: Reference, query_lower: str) -> bool:
    """Case-insensitive substring match across name, sm_name, description, purpose, tags, group."""
    for value in (ref.name, ref.sm_name, ref.description, ref.purpose, ref.group):
        if query_lower in value.lower():
            return True
    for key, val in ref.tags.items():
        if query_lower in key.lower() or query_lower in str(val).lower():
            return True
    return False


def search_references(
    registry: Registry,
    query: str,
    *,
    project: Optional[str] = None,
    provider: Optional[str] = None,
    env: Optional[str] = None,
    state: Optional[str] = None,
) -> List[Reference]:
    """Search references by free-text query with optional scope filters.

    Matches case-insensitively across name, sm_name, description, purpose,
    tags (keys and values), and group. An empty query always returns an empty
    list. Never touches a backend or fetches a value.

    Results sorted: enabled state first, then alphabetical by name.
    """
    if not query:
        return []

    query_lower = query.lower()
    results: List[Reference] = []
    for ref in registry:
        if project is not None and ref.project != project:
            continue
        if provider is not None and ref.provider != provider:
            continue
        if env is not None and ref.env != env:
            continue
        if state is not None and ref.state != state:
            continue
        if _matches_query(ref, query_lower):
            results.append(ref)

    results.sort(key=lambda r: (r.state != "enabled", r.name))
    return results
