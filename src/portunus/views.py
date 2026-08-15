"""Custom views -- named, human-curated collections of reference names for
ad-hoc task clustering (portunus-vault-trust-and-access Slice 4). Directly
answers "a custom view where I cluster the keys how I want as I prep them
for a project" -- task-shaped, not ownership-shaped, so it's deliberately
orthogonal to the structural org/project/env hierarchy (views.py) rather
than another facet of it.

Simplest possible v1 (design-discussion.md §4a): a named list of reference
names, not a saved tag-query -- a query-based "smart view" is real, larger,
future work, not needed to serve manual task-prep curation.

Every mutator here wraps its own load -> mutate -> save inside ONE flock
acquisition -- learned directly from vault-bindings.json's own retrofitted
lock (portunus-vault-backup epic): that file's save-only lock still leaves
its read-modify-write CLI callers racy. views.json gets it right from day
one instead of needing a second pass later.
"""
from __future__ import annotations

import json
import os
from dataclasses import dataclass, field
from pathlib import Path
from typing import Dict, List, Optional

from .filelock import flock_path
from .paths import home


class ViewError(RuntimeError):
    """Raised for a view operation that can't complete (unknown view name,
    duplicate creation, etc). Never carries a secret value -- views only
    ever hold reference names, never values."""


@dataclass
class View:
    name: str
    description: str = ""
    ref_names: List[str] = field(default_factory=list)


def _views_path(path: Optional[Path] = None) -> Path:
    return path or (home() / "views.json")


def _views_lock_path(path: Optional[Path] = None) -> Path:
    return _views_path(path).with_suffix(".lock")


def _load_unlocked(path: Optional[Path] = None) -> Dict[str, View]:
    views_path = _views_path(path)
    if not views_path.exists():
        return {}
    raw = json.loads(views_path.read_text() or "{}")
    return {
        name: View(
            name=name,
            description=cfg.get("description", ""),
            ref_names=list(cfg.get("ref_names", [])),
        )
        for name, cfg in raw.items()
    }


def _save_unlocked(views: Dict[str, View], path: Optional[Path] = None) -> None:
    views_path = _views_path(path)
    views_path.parent.mkdir(parents=True, exist_ok=True)
    raw = {
        name: {"description": v.description, "ref_names": v.ref_names}
        for name, v in views.items()
    }
    tmp = views_path.with_suffix(".json.tmp")
    tmp.write_text(json.dumps(raw, indent=2))
    os.chmod(tmp, 0o600)
    os.replace(tmp, views_path)
    os.chmod(views_path, 0o600)


def load_views(path: Optional[Path] = None) -> Dict[str, View]:
    """Plain read -- missing file means no views yet, returns {}. Unlocked,
    matching every other config-load's own posture in this codebase (a
    reader never observes a torn write; os.replace() is atomic)."""
    return _load_unlocked(path)


def create_view(name: str, description: str = "", path: Optional[Path] = None) -> View:
    with flock_path(_views_lock_path(path)):
        views = _load_unlocked(path)
        if name in views:
            raise ViewError(f"view already exists: {name!r}")
        view = View(name=name, description=description)
        views[name] = view
        _save_unlocked(views, path)
        return view


def delete_view(name: str, path: Optional[Path] = None) -> bool:
    with flock_path(_views_lock_path(path)):
        views = _load_unlocked(path)
        existed = views.pop(name, None) is not None
        if existed:
            _save_unlocked(views, path)
        return existed


def add_to_view(name: str, ref_name: str, path: Optional[Path] = None) -> View:
    """Idempotent -- adding a reference already in the view is a no-op, not
    a duplicate entry or an error."""
    with flock_path(_views_lock_path(path)):
        views = _load_unlocked(path)
        view = views.get(name)
        if view is None:
            raise ViewError(f"unknown view: {name!r}")
        if ref_name not in view.ref_names:
            view.ref_names.append(ref_name)
            _save_unlocked(views, path)
        return view


def remove_from_view(name: str, ref_name: str, path: Optional[Path] = None) -> View:
    """Idempotent -- removing a reference not in the view is a no-op, not
    an error."""
    with flock_path(_views_lock_path(path)):
        views = _load_unlocked(path)
        view = views.get(name)
        if view is None:
            raise ViewError(f"unknown view: {name!r}")
        if ref_name in view.ref_names:
            view.ref_names.remove(ref_name)
            _save_unlocked(views, path)
        return view
