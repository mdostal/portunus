"""The `org` field (portunus-vault-trust-and-access, Slice 1) -- an organizational
umbrella one level above `project`, using the same flat-structured-tag pattern
`provider`/`project`/`env`/`repo` already use. Additive: absent org never
breaks an existing reference or an existing caller that doesn't pass it."""
import pytest

from portunus import Registry
from portunus.registry import AmbiguousMatch


def test_add_accepts_org(home):
    reg = Registry()
    ref = reg.add("x", "sm-x", org="firefly-events", project="shindig")
    assert ref.org == "firefly-events"
    reloaded = Registry().require("x")
    assert reloaded.org == "firefly-events"


def test_add_without_org_defaults_to_empty_string(home):
    reg = Registry()
    ref = reg.add("x", "sm-x")
    assert ref.org == ""


def test_request_accepts_org(home):
    reg = Registry()
    ref = reg.request("new-secret", org="firefly-events", project="shindig")
    assert ref.org == "firefly-events"


def test_retag_updates_org_in_place(home):
    reg = Registry()
    reg.add("x", "sm-x", project="shindig")
    ref = reg.retag("x", org="firefly-events")
    assert ref.org == "firefly-events"
    reloaded = Registry().require("x")
    assert reloaded.org == "firefly-events"


def test_retag_without_org_leaves_it_unchanged(home):
    """Only-passed-fields-change: retagging some other field must not touch org."""
    reg = Registry()
    reg.add("x", "sm-x", org="firefly-events", project="shindig")
    ref = reg.retag("x", env="prod")
    assert ref.org == "firefly-events"
    assert ref.env == "prod"


def test_retag_to_identical_org_succeeds(home):
    reg = Registry()
    reg.add("x", "sm-x", org="firefly-events", project="shindig")
    ref = reg.retag("x", org="firefly-events")
    assert ref.org == "firefly-events"


def test_org_participates_in_collision_detection(home):
    """org joins provider/project/env/repo as a structural/identifying field
    -- two references retagged to an identical full combination (including
    org) collide, matching the existing precedent for those fields."""
    reg = Registry()
    reg.add("a", "sm-a", org="firefly-events", project="shindig", env="prod")
    reg.add("b", "sm-b", org="firefly-events", project="shindig", env="staging")
    with pytest.raises(AmbiguousMatch):
        reg.retag("b", env="prod")
    reloaded = Registry().require("b")
    assert reloaded.env == "staging"


def test_different_org_avoids_an_otherwise_identical_collision(home):
    """Two references sharing project+env but under DIFFERENT orgs must NOT
    collide -- org is part of the identity, not incidental."""
    reg = Registry()
    reg.add("a", "sm-a", org="firefly-events", project="shindig", env="prod")
    ref = reg.add("b", "sm-b", org="other-org", project="shindig", env="prod")
    assert ref.org == "other-org"


def test_resolve_by_tags_finds_by_org(home):
    reg = Registry()
    reg.add("x", "sm-x", org="firefly-events", project="shindig")
    reg.add("y", "sm-y", org="other-org", project="gig-tracker")
    found = reg.resolve_by_tags(org="firefly-events")
    assert found.name == "x"


def test_org_absent_on_pre_existing_references_is_never_an_error(home):
    """A reference registered before this field existed (org="") must keep
    working through every existing tag-based/list code path -- no migration
    required, matches every other optional structured field's own
    non-dropping precedent (tree --by's "(ungrouped)"/"(no repo set)")."""
    reg = Registry()
    reg.add("legacy-ref", "sm-legacy", project="shindig")
    ref = reg.require("legacy-ref")
    assert ref.org == ""
    # still findable by project even with no org set
    found = reg.resolve_by_tags(project="shindig")
    assert found.name == "legacy-ref"
