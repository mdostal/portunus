"""scripts/process_docs.py curates `.pHive/` into the docs site's
"Process & Design" section. These tests pin the curation rules (what is
published, what is deliberately left out) against both a synthetic tree and
the real repo, so a renamed artifact can't silently drop off the site."""
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT / "scripts"))

import process_docs  # noqa: E402


def _write(path: Path, text: str) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")
    return path


def _fake_repo(root: Path) -> Path:
    _write(root / ".pHive/CONTEXT.md", "# Project CONTEXT\n\nGlossary body.\n")
    _write(root / ".pHive/design/portunus-core/blueprint.html", "<title>BP</title>")
    epic = root / ".pHive/epics/portunus-mcp-server"
    _write(
        epic / "epic.yaml",
        "name: portunus-mcp-server\n"
        "title: Portunus MCP Server\n"
        "description: >\n"
        "  A local MCP stdio server\n"
        "  for other agents.\n"
        "methodology: tdd\n",
    )
    _write(
        epic / "docs/design-discussion.md",
        "# Design Discussion\n\n## 0. Decision\n\n```\n# not a heading\n```\n",
    )
    _write(epic / "docs/lessons-learned.md", "# Lessons\n\n## What broke\n")
    _write(epic / "docs/research-brief.md", "# Research\n")
    _write(epic / "docs/grill-record.md", "# Grill\n")
    _write(epic / "stories/01-a.yaml", "id: 01\n")
    # An epic with no design discussion is not a design record.
    _write(root / ".pHive/epics/portunus-no-design/epic.yaml", "title: Nope\n")
    return root


def test_read_epic_meta_handles_folded_scalars(tmp_path):
    root = _fake_repo(tmp_path)
    meta = process_docs.read_epic_meta(root / ".pHive/epics/portunus-mcp-server/epic.yaml")
    assert meta == {
        "title": "Portunus MCP Server",
        "description": "A local MCP stdio server for other agents.",
    }


def test_demote_headings_skips_fenced_code():
    out = process_docs.demote_headings("## A\n```\n# keep\n```\n#### B\n")
    assert out == "### A\n```\n# keep\n```\n##### B\n"


def test_nav_title_keeps_acronyms():
    assert process_docs.nav_title("portunus-oauth-token-broker") == "OAuth token broker"
    assert process_docs.nav_title("portunus-mcp-server") == "MCP server"
    assert process_docs.nav_title("portunus-session-ttl-and-list") == "Session TTL and list"


def test_collect_publishes_design_story_only(tmp_path):
    section = process_docs.collect(_fake_repo(tmp_path))

    assert [p.path for p in section.records] == ["process/records/mcp-server.md"]
    record = section.records[0].content
    assert record.startswith("# Portunus MCP Server\n")
    assert "A local MCP stdio server for other agents." in record
    assert "**Stories:** 1" in record
    assert "### 0. Decision" in record and "# not a heading" in record
    assert "## Lessons learned" in record and "### What broke" in record
    # Research briefs and grill records are churn, not the design story.
    assert "# Research" not in record and "# Grill" not in record

    paths = {p.path for p in section.pages}
    assert "process/context.md" in paths
    assert "process/visual/portunus-core-blueprint.html" in paths
    assert "process/visual/portunus-core-blueprint.md" in paths
    # Missing foundation sources are skipped, never an error.
    assert "process/standalone-core-outline.md" not in paths


def test_nav_shape(tmp_path):
    nav = process_docs.collect(_fake_repo(tmp_path)).nav()
    assert nav[0] == {"Overview": "process/index.md"}
    assert {"MCP server": "process/records/mcp-server.md"} in nav[2]["Design records"]
    # Raw HTML assets are published but never listed in the nav.
    assert all(not v.endswith(".html") for d in nav[1]["Foundations"] for v in d.values())


def test_real_repo_foundations_all_resolve():
    section = process_docs.collect(REPO_ROOT)
    expected = {f"process/{slug}.md" for _, slug, _ in process_docs.FOUNDATIONS}
    assert expected <= {p.path for p in section.foundations}
    assert section.records, "no .pHive design records found"
