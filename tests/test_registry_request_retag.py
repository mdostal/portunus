"""Registry.request() and Registry.retag() (story 01, portunus-agent-ops-federation)."""
import pytest

from portunus import Registry, Broker, AuditChain
from portunus.broker import NotInjectable
from portunus.registry import AmbiguousMatch, VALID_STATES


def test_requested_is_a_valid_state():
    assert "requested" in VALID_STATES


def test_request_creates_placeholder_with_no_value_stored(home):
    reg = Registry()
    ref = reg.request("new-secret", provider="vercel", project="mdostal.com", env="prod")
    assert ref.state == "requested"
    assert ref.name == "new-secret"
    assert not hasattr(ref, "value")
    reloaded = Registry().require("new-secret")
    assert reloaded.state == "requested"


def test_request_accepts_open_tags(home):
    reg = Registry()
    ref = reg.request("new-secret", tags={"team": "platform"})
    assert ref.tags == {"team": "platform"}


def test_requested_reference_fails_check_injectable_like_dropped_revoked(home):
    reg = Registry()
    reg.request("new-secret", provider="vercel")
    broker = Broker(reg, AuditChain())
    with pytest.raises(NotInjectable):
        broker.check_injectable("new-secret")


def test_enabled_and_locked_remain_injectable_after_broker_fix(home):
    """Regression guard for the check_injectable allowlist flip -- enabled/locked
    must still pass, only the fail-closed set should have grown."""
    reg = Registry()
    reg.add("x", "sm-x", state="enabled")
    reg.add("y", "sm-y", state="locked")
    broker = Broker(reg, AuditChain())
    assert broker.check_injectable("x").name == "x"
    assert broker.check_injectable("y").name == "y"


def test_retag_updates_tags_in_place(home):
    reg = Registry()
    reg.add("x", "sm-x", provider="vercel", project="mdostal.com", env="staging")
    ref = reg.retag("x", env="prod")
    assert ref.env == "prod"
    assert ref.name == "x"
    assert ref.sm_name == "sm-x"
    reloaded = Registry().require("x")
    assert reloaded.env == "prod"


def test_retag_to_identical_tags_succeeds(home):
    """Retagging to the reference's own current tags must not collide with itself."""
    reg = Registry()
    reg.add("x", "sm-x", provider="vercel", project="mdostal.com", env="prod")
    ref = reg.retag("x", provider="vercel", project="mdostal.com", env="prod")
    assert ref.env == "prod"


def test_retag_rejects_collision_with_different_reference(home):
    reg = Registry()
    reg.add("a", "sm-a", provider="vercel", project="mdostal.com", env="prod")
    reg.add("b", "sm-b", provider="vercel", project="mdostal.com", env="staging")
    with pytest.raises(AmbiguousMatch):
        reg.retag("b", env="prod")
    # no change made
    reloaded = Registry().require("b")
    assert reloaded.env == "staging"


def test_retag_open_tags_dict(home):
    reg = Registry()
    reg.add("x", "sm-x", tags={"team": "platform"})
    ref = reg.retag("x", tags={"team": "growth"})
    assert ref.tags == {"team": "growth"}
