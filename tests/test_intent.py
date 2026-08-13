"""parse_intent() -- deterministic natural-language -> tag-set mapping
(story 04). Fails closed: never guesses on ambiguity, only resolves when the
request unambiguously names known registry vocabulary."""
import pytest

from portunus import Registry
from portunus.intent import AmbiguousIntent, parse_intent


def _reg_with(home, *refs):
    reg = Registry()
    for kwargs in refs:
        reg.add(**kwargs)
    return reg


def test_parse_intent_resolves_unambiguous_provider_and_project(home):
    reg = _reg_with(
        home,
        dict(name="a", sm_name="sm-a", provider="vercel", project="mdostal.com", env="prod"),
        dict(name="b", sm_name="sm-b", provider="aws", project="other.com", env="prod"),
    )
    tags = parse_intent("the vercel secret for mdostal.com", reg)
    assert tags == {"provider": "vercel", "project": "mdostal.com"}


def test_parse_intent_resolves_env_when_mentioned(home):
    reg = _reg_with(
        home,
        dict(name="a", sm_name="sm-a", provider="vercel", project="mdostal.com", env="prod"),
    )
    tags = parse_intent("vercel secret for mdostal.com in prod", reg)
    assert tags == {"provider": "vercel", "project": "mdostal.com", "env": "prod"}


def test_parse_intent_raises_when_nothing_recognized(home):
    reg = _reg_with(
        home,
        dict(name="a", sm_name="sm-a", provider="vercel", project="mdostal.com"),
    )
    with pytest.raises(AmbiguousIntent) as exc_info:
        parse_intent("something about a totally unrelated thing", reg)
    assert exc_info.value.clarifying_question


def test_parse_intent_raises_on_conflicting_values_for_same_field(home):
    reg = _reg_with(
        home,
        dict(name="a", sm_name="sm-a", provider="vercel", project="mdostal.com"),
        dict(name="b", sm_name="sm-b", provider="aws", project="other.com"),
    )
    with pytest.raises(AmbiguousIntent) as exc_info:
        parse_intent("is it vercel or aws for this?", reg)
    assert set(exc_info.value.candidates) == {"vercel", "aws"}


def test_parse_intent_never_substring_matches(home):
    """"verc" must not match "vercel" -- same no-fuzzy-fallback contract as
    resolve_by_tags()."""
    reg = _reg_with(
        home,
        dict(name="a", sm_name="sm-a", provider="vercel", project="mdostal.com"),
    )
    with pytest.raises(AmbiguousIntent):
        parse_intent("verc something mdostal", reg)


def test_parse_intent_is_case_insensitive(home):
    reg = _reg_with(
        home,
        dict(name="a", sm_name="sm-a", provider="vercel", project="mdostal.com"),
    )
    tags = parse_intent("VERCEL secret for MDOSTAL.COM", reg)
    assert tags == {"provider": "vercel", "project": "mdostal.com"}


def test_parse_intent_never_includes_raw_request_text_in_result(home):
    reg = _reg_with(
        home,
        dict(name="a", sm_name="sm-a", provider="vercel", project="mdostal.com"),
    )
    request = "the vercel secret for mdostal.com please"
    tags = parse_intent(request, reg)
    assert request not in tags.values()
