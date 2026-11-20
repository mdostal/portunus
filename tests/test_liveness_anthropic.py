"""Anthropic liveness: LLM API-key probe vs Claude Code OAuth setup-token
expiry check (PANT-1132, GH #197)."""
import calendar
import json

from portunus import liveness
from portunus.liveness import (ANTHROPIC_MODELS_URL, DEAD, LIVE, UNKNOWN, AnthropicLivenessAdapter,
                               check_reference, checkable_references)

API_KEY = "sk-ant-api03-anthropic-do-not-leak-0xC0FFEE"
OAUTH = "sk-ant-oat01-claude-code-do-not-leak-0xBEEF"
ADMIN = "sk-ant-admin01-do-not-leak-0xF00D"
NOW = calendar.timegm((2026, 11, 20, 12, 0, 0))


class FakeTransport:
    def __init__(self, code=200):
        self.code = code
        self.calls = []

    def __call__(self, method, url, headers, timeout):
        self.calls.append((method, url, dict(headers)))
        return self.code


def _check(stack, name, value, code=200, tags=None):
    stack["registry"].add(name, f"sm-{name}", provider="anthropic", tags=tags)
    stack["backend"].set(f"sm-{name}", value)
    transport = FakeTransport(code)
    adapter = AnthropicLivenessAdapter(transport=transport, now=lambda: NOW)
    result = check_reference(stack["registry"].get(name), stack["resolver"], adapter=adapter)
    dumped = json.dumps(result.to_dict())
    assert value.strip() not in dumped
    assert value.strip() not in repr(result)
    return result, transport.calls


def test_registered():
    assert "anthropic" in liveness.liveness_providers()
    assert isinstance(liveness.liveness_adapter_for("anthropic"), AnthropicLivenessAdapter)


def test_api_key_probes_free_models_endpoint(stack):
    result, calls = _check(stack, "social-engine-prod-anthropic-api-key", API_KEY)
    assert result.status == LIVE
    assert calls == [("GET", ANTHROPIC_MODELS_URL,
                      {"x-api-key": API_KEY, "anthropic-version": "2023-06-01"})]
    assert ANTHROPIC_MODELS_URL.startswith("https://api.anthropic.com/v1/models")
    assert result.http_status == 200


def test_api_key_401_is_dead(stack):
    result, _ = _check(stack, "orchestration-dev-anthropic-api-key", API_KEY, code=401)
    assert result.status == DEAD and result.http_status == 401


def test_403_429_529_stay_unknown(stack):
    for i, code in enumerate((403, 429, 529, 500)):
        result, _ = _check(stack, f"k{i}-anthropic-api-key", API_KEY, code=code)
        assert result.status == UNKNOWN, code


def test_value_is_stripped_before_sending(stack):
    _, calls = _check(stack, "game-library-dev-anthropic-api-key", f"  {API_KEY}\n")
    assert calls[0][2]["x-api-key"] == API_KEY


def test_non_anthropic_value_is_not_sent(stack):
    result, calls = _check(stack, "x-anthropic-api-key", "sk-proj-openai-not-anthropic")
    assert calls == []
    assert result.status == UNKNOWN and "sk-ant-" in result.detail


def test_admin_key_is_not_probed(stack):
    result, calls = _check(stack, "org-anthropic-admin", ADMIN)
    assert calls == []
    assert result.status == UNKNOWN and "Admin API" in result.detail


def test_oauth_by_name_never_resolves_the_value(stack):
    result, calls = _check(stack, "claude-code-oauth", OAUTH, tags={"expires_at": "2027-10-01"})
    assert calls == []
    assert result.status == LIVE
    assert "not probed" in result.detail and "2027-10-01" in result.detail


def test_oauth_name_variant_with_suffix(stack):
    result, calls = _check(stack, "claude-code-oauth-ffevents-hive", OAUTH,
                           tags={"expires_at": "2027-10-01"})
    assert calls == [] and result.status == LIVE


def test_oauth_by_value_prefix_does_not_hit_api(stack):
    # Name says nothing; the sk-ant-oat prefix routes it to the expiry check.
    result, calls = _check(stack, "hive-anthropic-token", OAUTH, tags={"expires_at": "2027-10-01"})
    assert calls == []
    assert result.status == LIVE and "oauth-setup-token" in result.detail


def test_oauth_by_kind_tag(stack):
    result, calls = _check(stack, "hive-anthropic", OAUTH,
                           tags={"liveness_kind": "oauth-setup-token", "expires_at": "2027-10-01"})
    assert calls == [] and result.status == LIVE


def test_oauth_expired_is_dead(stack):
    result, _ = _check(stack, "claude-code-oauth", OAUTH, tags={"expires_at": "2026-11-19"})
    assert result.status == DEAD and "expired 2026-11-19" in result.detail


def test_oauth_expires_today_is_not_yet_dead(stack):
    result, _ = _check(stack, "claude-code-oauth", OAUTH, tags={"expires_at": "2026-11-20"})
    assert result.status == UNKNOWN and "in 0 days" in result.detail


def test_oauth_within_warning_window_is_unknown(stack):
    result, _ = _check(stack, "claude-code-oauth", OAUTH, tags={"expires_at": "2026-12-10"})
    assert result.status == UNKNOWN
    assert "claude setup-token" in result.detail


def test_oauth_iso_timestamp_accepted(stack):
    result, _ = _check(stack, "claude-code-oauth", OAUTH, tags={"expires_at": "2027-10-01T00:00:00Z"})
    assert result.status == LIVE


def test_oauth_missing_or_bad_tag_is_unknown(stack):
    result, _ = _check(stack, "claude-code-oauth", OAUTH)
    assert result.status == UNKNOWN and "'expires_at'" in result.detail
    result, _ = _check(stack, "claude-code-oauth-2", OAUTH,
                       tags={"liveness_kind": "oauth-setup-token", "expires_at": "next year"})
    assert result.status == UNKNOWN and "not a date" in result.detail


def test_sweep_includes_both_kinds(stack):
    for name in ("social-engine-dev-anthropic-api-key", "claude-code-oauth"):
        stack["registry"].add(name, f"sm-{name}", provider="anthropic")
    names = {r.name for r in checkable_references(stack["registry"])}
    assert {"social-engine-dev-anthropic-api-key", "claude-code-oauth"} <= names
