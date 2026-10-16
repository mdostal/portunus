"""Curate `.pHive/` planning artifacts into the docs site's "Process & Design" section.

`.pHive/` holds Hive's planning output: research briefs, design discussions,
grill records, horizontal/vertical plans, per-story YAML, and the occasional
HTML visual sidecar. Most of that is per-epic churn. This module picks out the
part that tells the *design story* and turns it into pages for the docs site:

- **Foundations**: the project context/glossary, the standalone-core
  structured outline, the Petitio RBAC research synthesis, and every HTML
  sidecar under `.pHive/design/` (published as-is, with a wrapper page).
- **Design records**: one page per epic that has a `design-discussion.md`,
  with that epic's `lessons-learned.md` / `cost-benefit-analysis.md` appended
  when they exist. New epics show up automatically on the next build.

Research briefs, grill records, plans and story YAML are deliberately left
out (they're still in the repo for anyone who wants them).

Pure stdlib with no mkdocs import, so it is unit-testable and portable to
another site generator. `scripts/mkdocs_process_hook.py` is the mkdocs glue.
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field
from pathlib import Path

REPO_URL = "https://github.com/mdostal/portunus"
SOURCE_REF = "dev"
SECTION_DIR = "process"

# (source path relative to repo root, page slug, nav title). Hand-picked:
# these are the design story's spine, not per-epic churn.
FOUNDATIONS = [
    (".pHive/CONTEXT.md", "context", "Project context & glossary"),
    (
        ".pHive/epics/portunus-standalone-core/docs/structured-outline.md",
        "standalone-core-outline",
        "Standalone core: structured outline",
    ),
    (
        ".pHive/research/petitio-rbac-synthesis.md",
        "petitio-rbac-synthesis",
        "Petitio RBAC: research synthesis",
    ),
]

# Appended to an epic's design record when present, in this order.
EPIC_EXTRAS = [
    ("lessons-learned.md", "Lessons learned"),
    ("cost-benefit-analysis.md", "Cost-benefit analysis"),
]

# Words that should keep their casing when an epic slug becomes a nav title.
_ACRONYMS = {"mcp": "MCP", "ui": "UI", "gcp": "GCP", "oauth": "OAuth", "ttl": "TTL",
             "cli": "CLI", "rbac": "RBAC", "l2": "L2"}

_FENCE = re.compile(r"^\s*(```|~~~)")
_HEADING = re.compile(r"^(#{1,5}) ")


@dataclass
class Page:
    """A generated docs file. `path` is relative to the docs dir."""

    path: str
    content: str | bytes
    title: str | None = None  # None: not listed in nav (e.g. a raw HTML asset)


@dataclass
class Section:
    pages: list[Page] = field(default_factory=list)
    foundations: list[Page] = field(default_factory=list)
    records: list[Page] = field(default_factory=list)

    def nav(self, overview: str = f"{SECTION_DIR}/index.md") -> list:
        """mkdocs-style nav entries for this section."""
        return [
            {"Overview": overview},
            {"Foundations": [{p.title: p.path} for p in self.foundations]},
            {"Design records": [{p.title: p.path} for p in self.records]},
        ]


def read_epic_meta(epic_yaml: Path) -> dict[str, str]:
    """Read the top-level `title`/`description`/`status` scalars from an epic.yaml.

    A deliberately tiny reader (plain and folded `>`/`|` scalars only) so this
    module has no PyYAML dependency; anything it can't parse is just omitted.
    """
    wanted = {"title", "description", "status"}
    meta: dict[str, str] = {}
    lines = epic_yaml.read_text(encoding="utf-8").splitlines()
    i = 0
    while i < len(lines):
        m = re.match(r"^([a-z_]+):\s*(.*)$", lines[i])
        i += 1
        if not m or m.group(1) not in wanted:
            continue
        key, value = m.group(1), m.group(2).strip()
        if value in (">", "|", ">-", "|-"):
            block = []
            while i < len(lines) and (lines[i].startswith(" ") or not lines[i].strip()):
                block.append(lines[i].strip())
                i += 1
            joiner = " " if value.startswith(">") else "\n"
            value = joiner.join(b for b in block if b).strip()
        meta[key] = value.strip("'\"")
    return meta


def demote_headings(markdown: str, levels: int = 1) -> str:
    """Push every ATX heading down `levels`, leaving fenced code alone."""
    out, in_fence = [], False
    for line in markdown.splitlines():
        if _FENCE.match(line):
            in_fence = not in_fence
        elif not in_fence:
            m = _HEADING.match(line)
            if m:
                line = "#" * min(len(m.group(1)) + levels, 6) + line[len(m.group(1)):]
        out.append(line)
    return "\n".join(out) + "\n"


def strip_title(markdown: str) -> str:
    """Drop a leading `# ...` H1 so the generated page controls its own title."""
    lines = markdown.lstrip("\n").splitlines()
    if lines and lines[0].startswith("# "):
        lines = lines[1:]
    return "\n".join(lines).lstrip("\n")


def nav_title(slug: str) -> str:
    """`portunus-oauth-token-broker` -> `OAuth token broker`."""
    words = slug.removeprefix("portunus-").split("-")
    words = [_ACRONYMS.get(w, w) for w in words]
    first = words[0]
    words[0] = first if first in _ACRONYMS.values() else first.capitalize()
    return " ".join(words)


def _source_url(rel: str) -> str:
    return f"{REPO_URL}/blob/{SOURCE_REF}/{rel}"


def _tree_url(rel: str) -> str:
    return f"{REPO_URL}/tree/{SOURCE_REF}/{rel}"


def _provenance(rel: str, kind: str) -> str:
    return (
        f'!!! note "Planning artifact"\n'
        f"    {kind} from [`{rel}`]({_source_url(rel)}), written by the Hive planning\n"
        f"    process before and during the build. It records intent and reasoning at\n"
        f"    the time; where it disagrees with the usage docs, the usage docs and the\n"
        f"    code are authoritative.\n\n"
    )


def _foundation(root: Path, rel: str, slug: str, title: str) -> Page | None:
    src = root / rel
    if not src.is_file():
        return None
    body = strip_title(src.read_text(encoding="utf-8"))
    content = f"# {title}\n\n{_provenance(rel, 'Published verbatim')}{body}"
    return Page(f"{SECTION_DIR}/{slug}.md", content, title)


def _sidecars(root: Path) -> list[Page]:
    pages: list[Page] = []
    design = root / ".pHive" / "design"
    if not design.is_dir():
        return pages
    for html in sorted(design.glob("*/*.html")):
        rel = html.relative_to(root).as_posix()
        epic = html.parent.name
        name = f"{epic}-{html.stem}"
        title = f"{html.stem.replace('-', ' ').capitalize()} ({epic.removeprefix('portunus-')})"
        pages.append(Page(f"{SECTION_DIR}/visual/{name}.html", html.read_bytes()))
        # The markdown link is resolved by mkdocs; the iframe src is raw HTML,
        # so it is written relative to the rendered page (visual/<name>/).
        wrapper = (
            f"# {title}\n\n"
            f"{_provenance(rel, 'An HTML visual-planning sidecar, published as-is')}"
            f"[Open full screen]({name}.html){{ .md-button }}\n\n"
            f'<iframe src="../{name}.html" title="{title}" loading="lazy" '
            f'style="width:100%;height:80vh;border:1px solid var(--md-default-fg-color--lightest);'
            f'border-radius:4px;background:#fff"></iframe>\n'
        )
        pages.append(Page(f"{SECTION_DIR}/visual/{name}.md", wrapper, title))
    return pages


def _record(root: Path, epic_dir: Path) -> Page | None:
    docs = epic_dir / "docs"
    discussion = docs / "design-discussion.md"
    if not discussion.is_file():
        return None
    meta = read_epic_meta(epic_dir / "epic.yaml") if (epic_dir / "epic.yaml").is_file() else {}
    heading = meta.get("title") or epic_dir.name
    heading = heading[:1].upper() + heading[1:]
    rel_dir = epic_dir.relative_to(root).as_posix()
    stories = len(list((epic_dir / "stories").glob("*"))) if (epic_dir / "stories").is_dir() else 0

    parts = [f"# {heading}\n"]
    if meta.get("description"):
        parts.append(f"{meta['description']}\n")
    facts = [f"**Epic:** `{epic_dir.name}`", f"**Stories:** {stories}"]
    if meta.get("status"):
        facts.append(f"**Status:** {meta['status']}")
    facts.append(f"**Full planning folder:** [`{rel_dir}`]({_tree_url(rel_dir)})")
    parts.append(" · ".join(facts) + "\n")
    parts.append(_provenance(discussion.relative_to(root).as_posix(), "The design discussion"))
    parts.append("## Design discussion\n")
    parts.append(demote_headings(strip_title(discussion.read_text(encoding="utf-8"))))
    for name, label in EPIC_EXTRAS:
        extra = docs / name
        if extra.is_file():
            parts.append(f"\n## {label}\n")
            parts.append(demote_headings(strip_title(extra.read_text(encoding="utf-8"))))
    slug = epic_dir.name.removeprefix("portunus-")
    return Page(f"{SECTION_DIR}/records/{slug}.md", "\n".join(parts), nav_title(epic_dir.name))


def collect(root: Path) -> Section:
    """Build the curated Process & Design section from `root/.pHive`."""
    root = Path(root)
    section = Section()
    for rel, slug, title in FOUNDATIONS:
        page = _foundation(root, rel, slug, title)
        if page:
            section.foundations.append(page)
    for page in _sidecars(root):
        if page.title:
            section.foundations.append(page)
        else:
            section.pages.append(page)
    epics = root / ".pHive" / "epics"
    if epics.is_dir():
        records = [r for r in (_record(root, d) for d in epics.iterdir() if d.is_dir()) if r]
        section.records = sorted(records, key=lambda p: p.title.lower())
    section.pages.extend(section.foundations)
    section.pages.extend(section.records)
    return section
