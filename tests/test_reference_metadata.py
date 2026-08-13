"""Reference metadata fields (description/purpose/injected_as) -- additive, non-tag-matchable."""
import json

from portunus import Registry, Reference
from portunus.cli import main
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


def test_cli_reg_add_accepts_description_and_purpose(home):
    rc = main(["reg", "add", "x", "sm-x", "--description", "a key", "--purpose", "billing"])
    assert rc == 0
    ref = Registry().require("x")
    assert ref.description == "a key"
    assert ref.purpose == "billing"


def test_cli_drop_accepts_description_purpose_and_injected_as(home, capsys):
    value_file = home / "value.txt"
    value_file.write_text("s3kr3t\n")
    rc = main([
        "drop", "x", "sm-x",
        "--description", "a key",
        "--injected-as", "prod=env:STRIPE_KEY,staging=file:.env.staging",
        "--value-file", str(value_file),
    ])
    assert rc == 0
    ref = Registry().require("x")
    assert ref.description == "a key"
    assert ref.injected_as == {"prod": "env:STRIPE_KEY", "staging": "file:.env.staging"}


def test_registry_retag_updates_description_purpose_injected_as_only(home):
    reg = Registry()
    reg.add("x", "sm-x", provider="vercel", project="mdostal.com")
    ref = reg.retag("x", description="new desc", purpose="new purpose",
                     injected_as={"prod": "env:X"})
    assert ref.description == "new desc"
    assert ref.purpose == "new purpose"
    assert ref.injected_as == {"prod": "env:X"}
    assert ref.provider == "vercel"
    assert ref.project == "mdostal.com"


def test_registry_retag_metadata_fields_never_trigger_collision_check(home):
    """description/purpose/injected_as are not in _STRUCTURED_TAG_FIELDS -- retagging
    them on one reference must never raise AmbiguousMatch against another reference
    that happens to share the same description text."""
    reg = Registry()
    reg.add("a", "sm-a", description="shared text")
    reg.add("b", "sm-b")
    ref = reg.retag("b", description="shared text")
    assert ref.description == "shared text"


def test_cli_retag_injected_as_flag_parses_colon_containing_values(home):
    reg = Registry()
    reg.add("x", "sm-x")
    rc = main(["retag", "x", "--injected-as", "prod=env:STRIPE_KEY"])
    assert rc == 0
    ref = Registry().require("x")
    assert ref.injected_as == {"prod": "env:STRIPE_KEY"}


def test_reference_group_and_related_default_empty():
    ref = Reference(name="x", sm_name="dostal-x")
    d = ref.to_dict()
    assert d["group"] == ""
    assert d["related"] == []


def test_registry_add_accepts_group_and_related(home):
    reg = Registry()
    ref = reg.add("x", "sm-x", group="project-y/supabase/auth", related=["mongo-key"])
    assert ref.group == "project-y/supabase/auth"
    assert ref.related == ["mongo-key"]


def test_registry_retag_updates_group_and_related_only(home):
    reg = Registry()
    reg.add("x", "sm-x", provider="vercel")
    ref = reg.retag("x", group="new/path", related=["a", "b"])
    assert ref.group == "new/path"
    assert ref.related == ["a", "b"]
    assert ref.provider == "vercel"


def test_registry_retag_group_related_never_trigger_collision_check(home):
    reg = Registry()
    reg.add("a", "sm-a", group="shared/group")
    reg.add("b", "sm-b")
    ref = reg.retag("b", group="shared/group")
    assert ref.group == "shared/group"


def test_legacy_registry_file_without_group_related_keys_still_loads(home):
    import json
    reg = Registry()
    legacy_raw = {
        "x": {
            "name": "x", "sm_name": "dostal-x", "scope": "", "kind": "",
            "state": "enabled", "approval": "", "sm_path": "",
            "provider": "", "project": "", "env": "", "tags": {},
            "description": "", "purpose": "", "injected_as": {},
        }
    }
    reg.path.parent.mkdir(parents=True, exist_ok=True)
    reg.path.write_text(json.dumps(legacy_raw))
    reg2 = Registry()
    ref = reg2.require("x")
    assert ref.group == ""
    assert ref.related == []


def test_group_and_related_are_not_structured_tag_fields():
    assert "group" not in _STRUCTURED_TAG_FIELDS
    assert "related" not in _STRUCTURED_TAG_FIELDS
