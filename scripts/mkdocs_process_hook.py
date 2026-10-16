"""mkdocs hook: publish the curated `.pHive/` design story as "Process & Design".

All curation logic lives in `process_docs.py` next to this file
(stdlib-only, unit tested). This file only adapts it to mkdocs: the generated pages are added as
in-memory files (nothing is written into `docs/`), and the section's nav is
inserted where `mkdocs.yml` has the `Process & Design: process/index.md`
placeholder entry.
"""
from __future__ import annotations

import sys
from pathlib import Path

from mkdocs.structure.files import File

sys.path.insert(0, str(Path(__file__).resolve().parent))
import process_docs  # noqa: E402

_ROOT = Path(__file__).resolve().parents[1]

NAV_TITLE = "Process & Design"
_section = None


def on_config(config):
    global _section
    _section = process_docs.collect(_ROOT)
    nav = config.get("nav") or []
    for i, entry in enumerate(nav):
        if isinstance(entry, dict) and NAV_TITLE in entry:
            nav[i] = {NAV_TITLE: _section.nav(overview=entry[NAV_TITLE])}
    return config


def on_files(files, config):
    for page in _section.pages:
        files.append(File.generated(config, page.path, content=page.content))
    return files
