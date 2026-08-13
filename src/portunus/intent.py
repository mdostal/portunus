"""Semantic front door: deterministic natural-language -> tag-set mapping.

parse_intent() is NOT a general NLP model -- it matches known vocabulary
already present in the registry's own provider/project/env fields against
the request text, whole-value only (never substring/fuzzy). It fails closed
(raises AmbiguousIntent) whenever it can't confidently determine a single
value for a field, or the request names nothing recognizable at all.

Downstream ambiguity -- the resolved tag set still matching more than one
reference (e.g. env wasn't mentioned and two references differ only by env)
-- is resolve_by_tags()'s job (AmbiguousMatch), not this module's. This
module only turns text into tags; Registry.resolve_by_tags() turns tags into
(at most) one Reference.
"""
from __future__ import annotations

import re
from typing import List, Optional

_VOCAB_FIELDS = ("provider", "project", "env")


class AmbiguousIntent(Exception):
    """parse_intent() could not confidently map text to a single tag set.

    Never a guess -- the caller should surface `clarifying_question` (and
    `candidates`, when non-empty) back to the requester rather than proceed.
    """

    def __init__(self, clarifying_question: str, candidates: Optional[List[str]] = None):
        self.clarifying_question = clarifying_question
        self.candidates = candidates or []
        super().__init__(clarifying_question)


def _collect_vocabulary(registry) -> dict:
    vocab = {field: set() for field in _VOCAB_FIELDS}
    for ref in registry:
        for field in _VOCAB_FIELDS:
            value = getattr(ref, field, "")
            if value:
                vocab[field].add(value)
    return vocab


def parse_intent(text: str, registry) -> dict:
    """Map `text` to a partial tag dict using vocabulary drawn from `registry`.

    Raises AmbiguousIntent if the text doesn't unambiguously name exactly one
    value per recognized field, or names nothing recognizable at all.
    """
    vocab = _collect_vocabulary(registry)
    text_l = text.lower()
    resolved: dict = {}

    for field, values in vocab.items():
        found = sorted({v for v in values if re.search(rf"\b{re.escape(v.lower())}\b", text_l)})
        if len(found) > 1:
            raise AmbiguousIntent(
                f"request mentions multiple possible {field} values: {found} -- "
                "please specify which one explicitly",
                candidates=found,
            )
        if len(found) == 1:
            resolved[field] = found[0]

    if not resolved:
        raise AmbiguousIntent(
            "could not confidently map this request to any known provider/project/env -- "
            "please specify explicit tags (e.g. provider=vercel,project=mdostal.com)",
            candidates=[],
        )
    return resolved
