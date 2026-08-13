"""Reference metadata fields (description/purpose/injected_as) -- additive, non-tag-matchable."""
import json

from portunus import Registry, Reference
from portunus.registry import _STRUCTURED_TAG_FIELDS


def test_metadata_fields_default_empty():
    ref = Reference(name="x", sm_name="dostal-x")
    d = ref.to_dict()
    assert d["description"] == ""
    assert d["purpose"] == ""
    assert d["injected_as"] == {}


def test_metadata_round_trips_through_persistence(home):
    reg = Registry()
    reg.add(
        "x", "dostal-x",
        description="Stripe billing API key",
        purpose="Charges customers for subscriptions",
        injected_as={"prod": "env:STRIPE_KEY", "staging": "file:.env.staging"},
    )
    reg2 = Registry()
    ref = reg2.require("x")
    assert ref.description == "Stripe billing API key"
    assert ref.purpose == "Charges customers for subscriptions"
    assert ref.injected_as == {"prod": "env:STRIPE_KEY", "staging": "file:.env.staging"}


def test_legacy_registry_file_without_new_fields_still_loads(home):
    reg = Registry()
    legacy_raw = {
        "x": {
            "name": "x", "sm_name": "dostal-x", "scope": "", "kind": "",
            "state": "enabled", "approval": "", "sm_path": "",
            "provider": "", "project": "", "env": "", "tags": {},
        }
    }
    reg.path.parent.mkdir(parents=True, exist_ok=True)
    reg.path.write_text(json.dumps(legacy_raw))

    reg2 = Registry()
    ref = reg2.require("x")
    assert ref.description == ""
    assert ref.purpose == ""
    assert ref.injected_as == {}


def test_description_and_purpose_are_not_structured_tag_fields():
    assert "description" not in _STRUCTURED_TAG_FIELDS
    assert "purpose" not in _STRUCTURED_TAG_FIELDS


def test_resolve_by_tags_does_not_match_on_description_or_purpose(home):
    reg = Registry()
    reg.add("x", "dostal-x", description="matches nothing", purpose="matches nothing")
    # description/purpose are not tag-matchable -- passing them as a "tag" key
    # falls through to the open tags{} dict, which is empty, so nothing matches.
    import pytest
    from portunus.registry import NoMatch
    with pytest.raises(NoMatch):
        reg.resolve_by_tags(description="matches nothing")
