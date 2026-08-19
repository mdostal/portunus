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


def test_cli_reg_add_accepts_group(home):
    rc = main(["reg", "add", "x", "sm-x", "--group", "project-y/supabase/auth"])
    assert rc == 0
    ref = Registry().require("x")
    assert ref.group == "project-y/supabase/auth"


def test_cli_drop_accepts_group_and_related(home):
    value_file = home / "value.txt"
    value_file.write_text("s3kr3t\n")
    rc = main([
        "drop", "x", "sm-x",
        "--group", "project-y/mongodb",
        "--related", "project-y-supabase-auth,project-y-other",
        "--value-file", str(value_file),
    ])
    assert rc == 0
    ref = Registry().require("x")
    assert ref.group == "project-y/mongodb"
    assert ref.related == ["project-y-supabase-auth", "project-y-other"]


def test_cli_retag_group_and_related(home):
    reg = Registry()
    reg.add("x", "sm-x")
    rc = main(["retag", "x", "--related", " name1 , name2 "])
    assert rc == 0
    ref = Registry().require("x")
    assert ref.related == ["name1", "name2"]


def test_cli_retag_related_empty_string_is_empty_list_not_error(home):
    reg = Registry()
    reg.add("x", "sm-x", related=["old"])
    rc = main(["retag", "x", "--related", ""])
    assert rc == 0
    ref = Registry().require("x")
    # empty --related means "not passed" (argparse default ""), same as --tags
    assert ref.related == ["old"]


def test_parse_related_drops_blank_entries():
    from portunus.cli import _parse_related
    assert _parse_related("a,,b") == ["a", "b"]


def test_parse_related_trims_whitespace():
    from portunus.cli import _parse_related
    assert _parse_related(" name1 , name2 ") == ["name1", "name2"]


# --- story 01 (portunus-vault-routing): Reference.backend override field ---

def test_reference_backend_defaults_empty():
    ref = Reference(name="x", sm_name="dostal-x")
    assert ref.backend == ""


def test_registry_add_accepts_backend(home):
    reg = Registry()
    ref = reg.add("x", "sm-x", backend="local")
    assert ref.backend == "local"


def test_registry_retag_updates_backend(home):
    reg = Registry()
    reg.add("x", "sm-x")
    ref = reg.retag("x", backend="gcp")
    assert ref.backend == "gcp"


def test_registry_retag_backend_never_triggers_collision_check(home):
    """backend is not in _STRUCTURED_TAG_FIELDS -- retagging it must never
    raise AmbiguousMatch against another reference that happens to share the
    same backend value."""
    reg = Registry()
    reg.add("a", "sm-a", backend="local")
    reg.add("b", "sm-b")
    ref = reg.retag("b", backend="local")
    assert ref.backend == "local"


# --- story 01 (portunus-provenance-graph): repo (structured) + source_files (list) ---

def test_reference_repo_and_source_files_default_empty():
    ref = Reference(name="x", sm_name="dostal-x")
    d = ref.to_dict()
    assert d["repo"] == ""
    assert d["source_files"] == []


def test_repo_is_a_structured_tag_field():
    assert "repo" in _STRUCTURED_TAG_FIELDS


def test_source_files_is_not_a_structured_tag_field():
    assert "source_files" not in _STRUCTURED_TAG_FIELDS


def test_registry_add_accepts_repo_and_source_files(home):
    reg = Registry()
    ref = reg.add("x", "sm-x", repo="event-api", source_files=["docker-compose.yml", "ci.yml"])
    assert ref.repo == "event-api"
    assert ref.source_files == ["docker-compose.yml", "ci.yml"]


def test_registry_retag_updates_repo_and_source_files(home):
    reg = Registry()
    reg.add("x", "sm-x", provider="vercel")
    ref = reg.retag("x", repo="event-api", source_files=["a.yml"])
    assert ref.repo == "event-api"
    assert ref.source_files == ["a.yml"]
    assert ref.provider == "vercel"


def test_find_by_tags_repo(home):
    reg = Registry()
    reg.add("x", "sm-x", repo="event-api")
    ref = reg.resolve_by_tags(repo="event-api")
    assert ref.name == "x"


def test_source_files_never_triggers_collision_check(home):
    reg = Registry()
    reg.add("a", "sm-a", source_files=["shared.yml"])
    reg.add("b", "sm-b")
    ref = reg.retag("b", source_files=["shared.yml"])
    assert ref.source_files == ["shared.yml"]


def test_two_references_differing_only_by_repo_do_not_collide(home):
    """repo IS a structured tag field -- two references identical on every
    other structured field but different repo must be distinguishable, not
    ambiguous, proving repo genuinely participates in the collision-check
    identity tuple the same way provider/project/env/scope/kind do."""
    reg = Registry()
    reg.add("a", "sm-a", provider="gcp", project="demo-cicd", repo="event-api")
    reg.add("b", "sm-b", provider="gcp", project="demo-cicd", repo="social-engine")
    ref_a = reg.resolve_by_tags(provider="gcp", project="demo-cicd", repo="event-api")
    ref_b = reg.resolve_by_tags(provider="gcp", project="demo-cicd", repo="social-engine")
    assert ref_a.name == "a"
    assert ref_b.name == "b"


def test_legacy_registry_file_without_repo_source_files_keys_still_loads(home):
    reg = Registry()
    legacy_raw = {
        "x": {
            "name": "x", "sm_name": "dostal-x", "scope": "", "kind": "",
            "state": "enabled", "approval": "", "sm_path": "",
            "provider": "", "project": "", "env": "", "tags": {},
            "description": "", "purpose": "", "injected_as": {},
            "group": "", "related": [], "backend": "",
        }
    }
    reg.path.parent.mkdir(parents=True, exist_ok=True)
    reg.path.write_text(json.dumps(legacy_raw))
    reg2 = Registry()
    ref = reg2.require("x")
    assert ref.repo == ""
    assert ref.source_files == []


def test_cli_reg_add_accepts_repo(home):
    rc = main(["reg", "add", "x", "sm-x", "--repo", "event-api"])
    assert rc == 0
    ref = Registry().require("x")
    assert ref.repo == "event-api"


def test_cli_drop_accepts_repo(home):
    value_file = home / "value.txt"
    value_file.write_text("s3kr3t\n")
    rc = main([
        "drop", "x", "sm-x", "--repo", "event-api", "--value-file", str(value_file),
    ])
    assert rc == 0
    ref = Registry().require("x")
    assert ref.repo == "event-api"


def test_cli_retag_repo_and_source_files(home):
    reg = Registry()
    reg.add("x", "sm-x")
    rc = main(["retag", "x", "--repo", "event-api", "--source-files", "a.yml,b.yml"])
    assert rc == 0
    ref = Registry().require("x")
    assert ref.repo == "event-api"
    assert ref.source_files == ["a.yml", "b.yml"]
