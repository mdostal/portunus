"""Linear API key liveness probe (PANT-1133, GH #196)."""
import json

import pytest

from portunus import liveness
from portunus.liveness import DEAD, LIVE, UNKNOWN, LinearLivenessAdapter, check_reference

PERSONAL = "lin_api_linear-do-not-leak-0xBEEF"
OAUTH = "lin_oauth_linear-do-not-leak-0xCAFE"
NAME = "event-api-prod-linear-api-key"
URL = "https://api.linear.app/graphql"


class FakeTransport:
    def __init__(self, code=200):
        self.code = code
        self.calls = []

    def __call__(self, method, url, headers, body, timeout):
        self.calls.append((method, url, dict(headers), body))
        return self.code


def _check(stack, value=PERSONAL, code=200):
    stack["registry"].add(NAME, f"sm-{NAME}", provider="linear")
    stack["backend"].set(f"sm-{NAME}", value)
    transport = FakeTransport(code)
    result = check_reference(stack["registry"].get(NAME), stack["resolver"],
                             adapter=LinearLivenessAdapter(transport=transport))
    assert value.strip() not in json.dumps(result.to_dict())
    assert value.strip() not in repr(result)
    return result, transport.calls


def test_registered_and_swept(stack):
    assert "linear" in liveness.liveness_providers()
    assert isinstance(liveness.liveness_adapter_for("linear"), LinearLivenessAdapter)
    stack["registry"].add(NAME, f"sm-{NAME}", provider="linear")
    assert [r.name for r in liveness.checkable_references(stack["registry"])] == [NAME]


def test_personal_key_sent_raw_with_viewer_query(stack):
    result, calls = _check(stack)
    assert result.status == LIVE
    assert calls == [("POST", URL, {"Authorization": PERSONAL, "Content-Type": "application/json"},
                      b'{"query": "{ viewer { id } }"}')]
    assert result.detail == f"personal API key: POST {URL} -> 200"


def test_oauth_token_sent_as_bearer(stack):
    result, calls = _check(stack, value=OAUTH)
    assert result.status == LIVE
    assert calls[0][2]["Authorization"] == f"Bearer {OAUTH}"
    assert result.detail.startswith("OAuth access token:")


def test_whitespace_around_value_is_stripped(stack):
    _, calls = _check(stack, value=f"  {PERSONAL}\n")
    assert calls[0][2]["Authorization"] == PERSONAL


def test_401_is_dead(stack):
    result, _ = _check(stack, code=401)
    assert result.status == DEAD
    assert result.http_status == 401


@pytest.mark.parametrize("code", [400, 403, 429, 500, 503])
def test_other_failures_are_unknown(stack, code):
    result, _ = _check(stack, code=code)
    assert result.status == UNKNOWN
    assert result.http_status == code


def test_unprefixed_value_is_not_sent(stack):
    result, calls = _check(stack, value="some-unprefixed-token-0xD00D")
    assert calls == []
    assert result.status == UNKNOWN
    assert "not probed" in result.detail
    assert result.http_status is None


def test_network_failure_is_unknown(stack):
    def boom(*_args):
        raise liveness.LivenessError("request failed: URLError")

    stack["registry"].add(NAME, f"sm-{NAME}", provider="linear")
    stack["backend"].set(f"sm-{NAME}", PERSONAL)
    result = check_reference(stack["registry"].get(NAME), stack["resolver"],
                             adapter=LinearLivenessAdapter(transport=boom))
    assert result.status == UNKNOWN
    assert PERSONAL not in result.detail
