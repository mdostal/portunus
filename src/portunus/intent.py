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

# Narrow, explicit keyword sets for intent_kind classification. Whole-word
# matching only. False-negative-to-fetch (a real add/rotate phrasing that
# isn't recognized) is the accepted failure mode -- it just means nothing
# new happens. False-positive-to-add/rotate is what must stay rare, so the
# list stays small and literal rather than trying to be clever.
_ADD_KEYWORDS = ("add", "create", "new secret")
_ROTATE_KEYWORDS = ("rotate", "roll", "regenerate")


class ParsedIntent(dict):
    """A resolved tag dict -- unpack directly via **result, unchanged from
    parse_intent()'s original contract -- that also carries the classified
    intent_kind ("fetch" | "add" | "rotate") as an attribute."""

    def __init__(self, tags: dict, intent_kind: str):
        super().__init__(tags)
        self.intent_kind = intent_kind


def classify_intent_kind(text_l: str) -> str:
    """Classify already-lowercased text as 'fetch' | 'add' | 'rotate'.

    Public so callers that need the kind before vocabulary-matched tags are
    even meaningful (e.g. an 'add' request naming brand-new tags with no
    existing vocabulary to match against) can classify first, independent
    of parse_intent()'s registry-vocabulary lookup.
    """
    is_add = any(re.search(rf"\b{re.escape(kw)}\b", text_l) for kw in _ADD_KEYWORDS)
    is_rotate = any(re.search(rf"\b{re.escape(kw)}\b", text_l) for kw in _ROTATE_KEYWORDS)
    if is_add and is_rotate:
        raise AmbiguousIntent(
            "request mentions both add and rotate language -- please specify one",
            candidates=["add", "rotate"],
        )
    if is_add:
        return "add"
    if is_rotate:
        return "rotate"
    return "fetch"


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


def parse_intent(text: str, registry) -> ParsedIntent:
    """Map `text` to a ParsedIntent (a tags dict + .intent_kind) using
    vocabulary drawn from `registry`.

    Raises AmbiguousIntent if the text doesn't unambiguously name exactly one
    value per recognized field, names nothing recognizable at all, or names
    conflicting add/rotate language.

    Backward compatible: ParsedIntent IS a dict, so existing callers doing
    `registry.resolve_by_tags(**parse_intent(...))` are unaffected -- only
    callers that also want intent_kind need to change.
    """
    intent_kind = classify_intent_kind(text.lower())

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
    return ParsedIntent(resolved, intent_kind)
