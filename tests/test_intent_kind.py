"""intent_kind classification on parse_intent() (story 02,
portunus-agent-ops-federation). Backward compatible: the returned object is
still a plain dict for **unpacking (existing fetch call sites are
unaffected), with an added .intent_kind attribute."""
import pytest

from portunus import Registry
from portunus.intent import AmbiguousIntent, parse_intent


def _reg(home):
    reg = Registry()
    reg.add("a", "sm-a", provider="vercel", project="mdostal.com", env="prod")
    return reg


def test_default_intent_kind_is_fetch(home):
    result = parse_intent("the vercel secret for mdostal.com in prod", _reg(home))
    assert result.intent_kind == "fetch"


def test_result_still_unpacks_as_a_plain_tags_dict(home):
    """Regression guard: existing **tag_set call sites (resolve_by_tags(**result))
    must keep working unchanged."""
    result = parse_intent("the vercel secret for mdostal.com in prod", _reg(home))
    assert dict(result) == {"provider": "vercel", "project": "mdostal.com", "env": "prod"}
    reg = _reg(home)
    ref = reg.resolve_by_tags(**result)
    assert ref.name == "a"


def test_add_keyword_classifies_as_add(home):
    result = parse_intent("add a new secret for vercel mdostal.com", _reg(home))
    assert result.intent_kind == "add"


def test_create_keyword_classifies_as_add(home):
    result = parse_intent("create a secret for vercel mdostal.com", _reg(home))
    assert result.intent_kind == "add"


def test_rotate_keyword_classifies_as_rotate(home):
    result = parse_intent("rotate the vercel secret for mdostal.com", _reg(home))
    assert result.intent_kind == "rotate"


def test_roll_keyword_classifies_as_rotate(home):
    result = parse_intent("roll the vercel secret for mdostal.com in prod", _reg(home))
    assert result.intent_kind == "rotate"


def test_regenerate_keyword_classifies_as_rotate(home):
    result = parse_intent("regenerate the vercel secret for mdostal.com in prod", _reg(home))
    assert result.intent_kind == "rotate"


def test_conflicting_add_and_rotate_keywords_raise_ambiguous_intent(home):
    with pytest.raises(AmbiguousIntent):
        parse_intent("add or rotate the vercel secret for mdostal.com", _reg(home))


def test_keyword_matching_is_whole_word_not_substring(home):
    """"scroll" must not match "roll" -- same no-fuzzy-fallback discipline as
    tag matching."""
    result = parse_intent("scroll to the vercel secret for mdostal.com in prod", _reg(home))
    assert result.intent_kind == "fetch"


def test_list_keyword_classifies_as_list(home):
    result = parse_intent("list secrets for project mdostal.com", _reg(home))
    assert result.intent_kind == "list"


def test_what_secrets_phrase_classifies_as_list(home):
    result = parse_intent("what secrets are available for mdostal.com", _reg(home))
    assert result.intent_kind == "list"


def test_available_for_phrase_classifies_as_list(home):
    result = parse_intent("secrets available for vercel mdostal.com", _reg(home))
    assert result.intent_kind == "list"


def test_conflicting_list_and_add_keywords_raise_ambiguous_intent(home):
    with pytest.raises(AmbiguousIntent):
        parse_intent("add or list the vercel secret for mdostal.com", _reg(home))
