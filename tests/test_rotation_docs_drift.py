"""docs/rotation.md's capability matrix must not lag behind the code: every
adapter in rotation.py's registry that reports capability() == "auto" has to
appear as **auto** in that table (PANT-853 -- README/architecture once said
"nothing rotates yet" while two adapters were already real).

A provider may have several rows, one per credential subtype (Linear,
GitHub -- PANT-1141): the provider counts as documented-auto when ANY of
its rows says **auto**."""
import re
from pathlib import Path

from portunus import rotation

ROTATION_MD = Path(__file__).resolve().parent.parent / "docs" / "rotation.md"


def _doc_rows() -> dict:
    """Parse the provider capability matrix: provider -> {capabilities}."""
    rows = {}
    for line in ROTATION_MD.read_text().splitlines():
        m = re.match(r"^\|\s*`([a-z0-9_-]+)`[^|]*\|[^|]*\|\s*\**([a-z_ ]+?)\**\s*\|", line)
        if m:
            rows.setdefault(m.group(1), set()).add(m.group(2))
    return rows


def _doc_capabilities() -> dict:
    """provider -> "auto" if any of its rows is auto, else its one capability."""
    return {
        provider: "auto" if "auto" in caps else sorted(caps)[0]
        for provider, caps in _doc_rows().items()
    }


def _code_capabilities() -> dict:
    return {
        provider: rotation.rotation_adapter_for(provider).capability()
        for provider in rotation._ADAPTERS
    }


def test_matrix_parses():
    assert _doc_capabilities(), "could not find the provider capability matrix in docs/rotation.md"


def test_every_auto_adapter_is_documented_as_auto():
    code = _code_capabilities()
    docs = _doc_capabilities()
    auto = sorted(p for p, cap in code.items() if cap == "auto")
    assert auto, "expected at least one real (auto) rotation adapter"
    for provider in auto:
        assert docs.get(provider) == "auto", (
            f"{provider} adapter reports capability 'auto' but docs/rotation.md lists "
            f"{docs.get(provider)!r}"
        )


def test_docs_do_not_claim_auto_for_a_non_auto_adapter():
    code = _code_capabilities()
    for provider, cap in _doc_capabilities().items():
        if cap == "auto":
            assert code.get(provider) == "auto", (
                f"docs/rotation.md lists {provider} as auto but rotation.py reports {code.get(provider)!r}"
            )
