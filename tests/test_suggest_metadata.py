"""LLM-suggests / human-confirms metadata workflow (portunus-vault-trust-
and-access Slice 6). suggest_metadata() writes ONLY the sidecar; the live
field only ever changes via an explicit human confirm (which is just a
normal retag() call, per design-discussion.md §4 -- no second write path)."""
import pytest

from portunus import Registry
from portunus.registry import SUGGESTIBLE_FIELDS


def test_suggest_metadata_never_touches_the_live_field(home):
    reg = Registry()
    reg.add("x", "sm-x")
    ref = reg.suggest_metadata("x", by="claude-code", fields={"description": "a stripe key"})
    assert ref.description == ""  # live field untouched
    assert ref.suggested["description"]["value"] == "a stripe key"
    assert ref.suggested["description"]["by"] == "claude-code"
    assert "at" in ref.suggested["description"]


def test_suggest_metadata_rejects_routing_fields(home):
    reg = Registry()
    reg.add("x", "sm-x")
    with pytest.raises(ValueError):
        reg.suggest_metadata("x", by="claude-code", fields={"project": "shindig"})
    with pytest.raises(ValueError):
        reg.suggest_metadata("x", by="claude-code", fields={"org": "firefly-events"})
    with pytest.raises(ValueError):
        reg.suggest_metadata("x", by="claude-code", fields={"backend": "gcp"})


def test_all_suggestible_fields_are_accepted(home):
    reg = Registry()
    reg.add("x", "sm-x")
    ref = reg.suggest_metadata("x", by="claude-code", fields={
        "description": "a", "purpose": "b", "tags": {"team": "platform"}, "group": "c/d",
    })
    assert set(ref.suggested.keys()) == set(SUGGESTIBLE_FIELDS)


def test_confirm_is_a_real_retag_plus_clear(home):
    """The confirm workflow, exercised end to end: suggest -> retag (accept)
    -> clear_suggestion. Confirms the live field actually changes and the
    sidecar entry is gone afterward."""
    reg = Registry()
    reg.add("x", "sm-x")
    reg.suggest_metadata("x", by="claude-code", fields={"description": "a stripe key"})

    suggestion = reg.require("x").suggested["description"]
    ref = reg.retag("x", description=suggestion["value"])
    ref = reg.clear_suggestion("x", "description")

    assert ref.description == "a stripe key"
    assert "description" not in ref.suggested


def test_reject_clears_without_ever_touching_the_live_field(home):
    reg = Registry()
    reg.add("x", "sm-x")
    reg.suggest_metadata("x", by="claude-code", fields={"description": "a stripe key"})

    ref = reg.clear_suggestion("x", "description")
    assert ref.description == ""
    assert "description" not in ref.suggested


def test_clear_suggestion_is_a_no_op_when_nothing_pending(home):
    reg = Registry()
    reg.add("x", "sm-x")
    ref = reg.clear_suggestion("x", "description")
    assert ref.description == ""


def test_second_suggestion_for_the_same_field_overwrites_the_pending_one(home):
    reg = Registry()
    reg.add("x", "sm-x")
    reg.suggest_metadata("x", by="agent-a", fields={"description": "first guess"})
    ref = reg.suggest_metadata("x", by="agent-b", fields={"description": "second guess"})
    assert ref.suggested["description"]["value"] == "second guess"
    assert ref.suggested["description"]["by"] == "agent-b"
    # live field is untouched either way
    assert ref.description == ""


def test_suggestion_survives_a_reload(home):
    reg = Registry()
    reg.add("x", "sm-x")
    reg.suggest_metadata("x", by="claude-code", fields={"purpose": "billing"})
    reloaded = Registry().require("x")
    assert reloaded.suggested["purpose"]["value"] == "billing"
