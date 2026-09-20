"""scripts/install.sh (canonical) and docs/install.sh (a duplicate, kept so
mkdocs's own build copies it through to the deployed site on every
`mkdocs gh-deploy` -- see docs/install.sh's own header comment, and GitHub
issue #139 for why gh-pages is mkdocs-managed now and force-replaced on
every deploy) must never drift apart. Only their leading `#`-comment
headers are allowed to differ -- the actual script logic must be
byte-identical."""
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent


def _strip_leading_comment_block(text: str) -> str:
    lines = text.splitlines(keepends=True)
    i = 0
    while i < len(lines) and (lines[i].startswith("#") or lines[i].strip() == ""):
        i += 1
    return "".join(lines[i:])


def test_docs_install_sh_matches_canonical_scripts_install_sh():
    canonical = (REPO_ROOT / "scripts" / "install.sh").read_text()
    published = (REPO_ROOT / "docs" / "install.sh").read_text()
    assert _strip_leading_comment_block(canonical) == _strip_leading_comment_block(published)
