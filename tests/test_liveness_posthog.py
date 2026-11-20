"""PostHog liveness probe: personal API key vs project token (PANT-1130, GH #199)."""
import json

import pytest

from portunus import liveness
from portunus.liveness import (DEAD, LIVE, UNKNOWN, PostHogLivenessAdapter, check_reference,
                               checkable_references)

PERSONAL = "phx_posthog-personal-do-not-leak-0xC0FFEE"
PROJECT = "phc_posthog-project-do-not-leak-0xBEEF"
US, EU = "https://us.posthog.com", "https://eu.posthog.com"


class FakeTransport:
    """Answers by URL prefix (host); `codes` maps host -> status, default 200."""

    def __init__(self, codes=None, default=200):
        self.codes = codes or {}
        self.default = default
        self.calls = []

    def __call__(self, method, url, headers, body, timeout):
        self.calls.append((method, url, dict(headers), body))
        for host, code in self.codes.items():
            if url.startswith(host):
                return code
        return self.default


def _check(stack, name, value, codes=None, default=200, tags=None, provider="posthog"):
    stack["registry"].add(name, f"sm-{name}", provider=provider, tags=tags)
    stack["backend"].set(f"sm-{name}", value)
    transport = FakeTransport(codes, default)
    result = check_reference(stack["registry"].get(name), stack["resolver"],
                             adapter=PostHogLivenessAdapter(transport=transport))
    dumped = json.dumps(result.to_dict())
    assert value.strip() not in dumped
    assert value.strip() not in repr(result)
    return result, transport.calls


def test_registered():
    assert "posthog" in liveness.liveness_providers()
    assert isinstance(liveness.liveness_adapter_for("posthog"), PostHogLivenessAdapter)


def test_personal_key_probes_users_me_and_stops_at_first_live(stack):
    result, calls = _check(stack, "mdostal-personal-posthog-personal-key", PERSONAL)
    assert result.status == LIVE
    assert calls == [("GET", f"{US}/api/users/@me/", {"Authorization": f"Bearer {PERSONAL}"}, None)]
    assert result.detail == f"personal-key: GET {US}/api/users/@me/ -> 200"


def test_project_token_probes_flags_with_body_not_private_api(stack):
    result, calls = _check(stack, "mdostal-com-posthog-project-key", PROJECT)
    assert result.status == LIVE
    method, url, headers, body = calls[0]
    assert (method, url) == ("POST", f"{US}/flags/?v=2")
    assert "Authorization" not in headers
    assert json.loads(body) == {"api_key": PROJECT, "distinct_id": "portunus-liveness"}
    assert result.detail.startswith("project-key: POST")


def test_value_prefix_beats_name(stack):
    # A project token stored under a "personal-key" name is still a project token.
    result, calls = _check(stack, "x-posthog-personal-key", PROJECT)
    assert calls[0][0] == "POST"
    assert result.status == LIVE


def test_eu_key_live_after_us_401(stack):
    result, calls = _check(stack, "mercatus-liber-posthog-project-token", PROJECT, codes={US: 401})
    assert result.status == LIVE
    assert [c[1] for c in calls] == [f"{US}/flags/?v=2", f"{EU}/flags/?v=2"]
    assert result.http_status == 200


@pytest.mark.parametrize("value", [PERSONAL, PROJECT])
def test_dead_only_when_every_region_says_401(stack, value):
    result, calls = _check(stack, "ffe-posthog-api-key", value, default=401)
    assert result.status == DEAD
    assert len(calls) == 2


@pytest.mark.parametrize("eu_code", [403, 429, 500, 503])
def test_401_plus_other_failure_is_unknown(stack, eu_code):
    # A 403 on the personal route is a key without the user:read scope.
    result, _ = _check(stack, "ffe-posthog-api-key", PERSONAL, codes={US: 401, EU: eu_code})
    assert result.status == UNKNOWN


def test_host_tag_pins_one_host(stack):
    result, calls = _check(stack, "ffe-posthog-server-key", PERSONAL, default=401,
                           tags={"posthog_host": "https://posthog.internal.example/"})
    assert [c[1] for c in calls] == ["https://posthog.internal.example/api/users/@me/"]
    assert result.status == DEAD


@pytest.mark.parametrize("url", ["ftp://ph.example", "ph.example", "https://", "https://u:p@ph.example",
                                 "https://ph.example?x=1"])
def test_bad_host_tag_not_probed(stack, url):
    result, calls = _check(stack, "ffe-posthog-api-key", PERSONAL, tags={"posthog_host": url})
    assert calls == []
    assert result.status == UNKNOWN
    assert "posthog_host" in result.detail


def test_unprefixed_value_uses_name_kind(stack):
    result, calls = _check(stack, "legacy-posthog-project-token", "legacy-token-0xABC")
    assert calls[0][0] == "POST"
    assert result.status == LIVE


def test_unprefixed_value_uses_kind_tag(stack):
    result, calls = _check(stack, "legacy-posthog-api-key", "legacy-token-0xABC",
                           tags={"liveness_kind": "personal-key"})
    assert calls[0][0] == "GET"


def test_unidentifiable_value_not_sent(stack):
    result, calls = _check(stack, "ffe-posthog-api-key", "legacy-token-0xABC")
    assert calls == []
    assert result.status == UNKNOWN
    assert "phx_/phc_" in result.detail


def test_project_secret_key_not_sent(stack):
    result, calls = _check(stack, "ffe-posthog-server-key", "phs_secret-do-not-leak")
    assert calls == []
    assert result.status == UNKNOWN
    assert "phs_" in result.detail


@pytest.mark.parametrize("name", ["mdostal-com-posthog-host", "ffe-posthog-tracking"])
def test_config_references_not_probed_and_left_out_of_sweeps(stack, name):
    result, calls = _check(stack, name, "https://us.i.posthog.com")
    assert calls == []
    assert result.status == UNKNOWN
    assert name not in [r.name for r in checkable_references(stack["registry"])]


def test_credentials_included_in_sweeps(stack):
    stack["registry"].add("ffe-posthog-api-key", "sm-a", provider="posthog")
    assert "ffe-posthog-api-key" in [r.name for r in checkable_references(stack["registry"])]


def test_network_error_is_unknown(stack):
    def boom(*a):
        raise liveness.LivenessError("request failed: timeout")

    stack["registry"].add("ffe-posthog-api-key", "sm-x", provider="posthog")
    stack["backend"].set("sm-x", PERSONAL)
    result = check_reference(stack["registry"].get("ffe-posthog-api-key"), stack["resolver"],
                             adapter=PostHogLivenessAdapter(transport=boom))
    assert result.status == UNKNOWN
    assert PERSONAL not in json.dumps(result.to_dict())
