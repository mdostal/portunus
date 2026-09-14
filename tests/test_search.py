"""Unit tests for search.py — free-text search over the Registry."""
import pytest

from portunus import Registry
from portunus.search import search_references, _matches_query


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _add(reg, name, *, sm_name="", description="", purpose="", group="",
         tags=None, project="", provider="", env="", state="enabled"):
    reg.add(
        name, sm_name or name,
        description=description, purpose=purpose, group=group,
        tags=tags or {}, project=project, provider=provider, env=env,
        state=state,
    )
    return reg.require(name)


# ---------------------------------------------------------------------------
# Empty query
# ---------------------------------------------------------------------------

def test_empty_query_returns_empty_list(home):
    reg = Registry()
    _add(reg, "anthropic-key", description="Claude API key")
    assert search_references(reg, "") == []


def test_whitespace_only_query_returns_empty_list(home):
    reg = Registry()
    _add(reg, "stripe-key")
    assert search_references(reg, "   ") == []


# ---------------------------------------------------------------------------
# Field matching
# ---------------------------------------------------------------------------

def test_matches_name(home):
    reg = Registry()
    _add(reg, "shared-anthropic")
    results = search_references(reg, "anthropic")
    assert len(results) == 1
    assert results[0].name == "shared-anthropic"


def test_matches_sm_name(home):
    reg = Registry()
    _add(reg, "mykey", sm_name="DOSTAL_ANTHROPIC_KEY")
    results = search_references(reg, "dostal_anthropic")
    assert len(results) == 1
    assert results[0].name == "mykey"


def test_matches_description(home):
    reg = Registry()
    _add(reg, "stripe-billing", description="Stripe billing API key for subscriptions")
    results = search_references(reg, "billing api")
    assert len(results) == 1
    assert results[0].name == "stripe-billing"


def test_matches_purpose(home):
    reg = Registry()
    _add(reg, "linear-token", purpose="Charges customers via Linear integration")
    results = search_references(reg, "linear integration")
    assert len(results) == 1


def test_matches_group(home):
    reg = Registry()
    _add(reg, "supa-key", group="project-y/supabase/auth")
    results = search_references(reg, "supabase/auth")
    assert len(results) == 1


def test_matches_tag_value(home):
    reg = Registry()
    _add(reg, "discord-bot", tags={"service": "discord", "tier": "community"})
    results = search_references(reg, "discord")
    assert len(results) == 1


def test_matches_tag_key(home):
    reg = Registry()
    _add(reg, "some-key", tags={"anthropic_tier": "free"})
    results = search_references(reg, "anthropic_tier")
    assert len(results) == 1


# ---------------------------------------------------------------------------
# Case-insensitive matching
# ---------------------------------------------------------------------------

def test_case_insensitive_name(home):
    reg = Registry()
    _add(reg, "shared-anthropic")
    assert search_references(reg, "ANTHROPIC") != []
    assert search_references(reg, "Anthropic") != []
    assert search_references(reg, "anthropic") != []


def test_case_insensitive_description(home):
    reg = Registry()
    _add(reg, "gcp-key", description="Google Cloud Platform service key")
    assert search_references(reg, "GOOGLE CLOUD") != []


def test_case_insensitive_tags(home):
    reg = Registry()
    _add(reg, "slack-bot", tags={"platform": "Slack"})
    assert search_references(reg, "slack") != []


# ---------------------------------------------------------------------------
# No match
# ---------------------------------------------------------------------------

def test_no_match_returns_empty(home):
    reg = Registry()
    _add(reg, "stripe-key", description="Stripe payment key")
    results = search_references(reg, "anthropic")
    assert results == []


def test_empty_registry_returns_empty(home):
    reg = Registry()
    assert search_references(reg, "anything") == []


# ---------------------------------------------------------------------------
# Scope filters
# ---------------------------------------------------------------------------

def test_project_filter(home):
    reg = Registry()
    _add(reg, "flayr-key", description="api key", project="flayr")
    _add(reg, "janus-key", description="api key", project="janus")
    results = search_references(reg, "api key", project="flayr")
    assert len(results) == 1
    assert results[0].name == "flayr-key"


def test_provider_filter(home):
    reg = Registry()
    _add(reg, "gcp-key", description="cloud key", provider="gcp")
    _add(reg, "aws-key", description="cloud key", provider="aws")
    results = search_references(reg, "cloud key", provider="gcp")
    assert len(results) == 1
    assert results[0].name == "gcp-key"


def test_env_filter(home):
    reg = Registry()
    _add(reg, "prod-key", description="database key", env="prod")
    _add(reg, "dev-key", description="database key", env="dev")
    results = search_references(reg, "database key", env="dev")
    assert len(results) == 1
    assert results[0].name == "dev-key"


def test_state_filter(home):
    reg = Registry()
    _add(reg, "active-key", description="api token", state="enabled")
    _add(reg, "dropped-key", description="api token", state="dropped")
    results = search_references(reg, "api token", state="dropped")
    assert len(results) == 1
    assert results[0].name == "dropped-key"


def test_combined_filters(home):
    reg = Registry()
    _add(reg, "flayr-gcp-prod", description="key", project="flayr", provider="gcp", env="prod")
    _add(reg, "flayr-gcp-dev", description="key", project="flayr", provider="gcp", env="dev")
    _add(reg, "janus-gcp-prod", description="key", project="janus", provider="gcp", env="prod")
    results = search_references(reg, "key", project="flayr", provider="gcp", env="prod")
    assert len(results) == 1
    assert results[0].name == "flayr-gcp-prod"


def test_filter_with_no_match_returns_empty(home):
    reg = Registry()
    _add(reg, "stripe-key", description="payment key", project="flayr")
    results = search_references(reg, "payment key", project="janus")
    assert results == []


# ---------------------------------------------------------------------------
# Sorting: enabled first, then alphabetical by name
# ---------------------------------------------------------------------------

def test_enabled_sorted_before_non_enabled(home):
    reg = Registry()
    _add(reg, "beta-key", description="api key", state="requested")
    _add(reg, "alpha-key", description="api key", state="enabled")
    _add(reg, "gamma-key", description="api key", state="dropped")
    results = search_references(reg, "api key")
    assert results[0].name == "alpha-key"
    assert results[0].state == "enabled"
    non_enabled = [r.name for r in results[1:]]
    assert set(non_enabled) == {"beta-key", "gamma-key"}


def test_alphabetical_within_same_state(home):
    reg = Registry()
    _add(reg, "zebra-key", description="cloud service key", state="enabled")
    _add(reg, "apple-key", description="cloud service key", state="enabled")
    _add(reg, "mango-key", description="cloud service key", state="enabled")
    results = search_references(reg, "cloud service key")
    assert [r.name for r in results] == ["apple-key", "mango-key", "zebra-key"]


def test_alphabetical_among_non_enabled(home):
    reg = Registry()
    _add(reg, "z-old", description="token", state="dropped")
    _add(reg, "a-old", description="token", state="revoked")
    results = search_references(reg, "token")
    assert results[0].name == "a-old"
    assert results[1].name == "z-old"


# ---------------------------------------------------------------------------
# _matches_query unit tests
# ---------------------------------------------------------------------------

def test_matches_query_false_when_no_field_matches(home):
    reg = Registry()
    ref = _add(reg, "stripe-key", description="payment gateway", sm_name="STRIPE_KEY")
    assert not _matches_query(ref, "anthropic")


def test_matches_query_true_on_partial_name(home):
    reg = Registry()
    ref = _add(reg, "shared-anthropic-v2")
    assert _matches_query(ref, "anthropic")


def test_matches_query_true_on_tag_value(home):
    reg = Registry()
    ref = _add(reg, "myref", tags={"integration": "Linear"})
    assert _matches_query(ref, "linear")
