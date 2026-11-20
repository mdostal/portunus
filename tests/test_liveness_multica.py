"""Multica workspace PAT liveness probe (PANT-1128, GH #201)."""
import json

import pytest

from portunus import liveness
from portunus.liveness import DEAD, LIVE, UNKNOWN, MulticaLivenessAdapter, check_reference

SECRET = "mul_multica-do-not-leak-0xC0FFEE"
NAME = "dostal-tech-multica-pat-token"


class FakeTransport:
    def __init__(self, code=200):
        self.code = code
        self.calls = []

    def __call__(self, method, url, headers, timeout):
        self.calls.append((method, url, dict(headers)))
        return self.code


def _check(stack, value=SECRET, code=200, provider="multica", tags=None):
    stack["registry"].add(NAME, f"sm-{NAME}", provider=provider, tags=tags)
    stack["backend"].set(f"sm-{NAME}", value)
    transport = FakeTransport(code)
    result = check_reference(stack["registry"].get(NAME), stack["resolver"],
                             adapter=MulticaLivenessAdapter(transport=transport))
    assert value.strip() not in json.dumps(result.to_dict())
    assert value.strip() not in repr(result)
    return result, transport.calls


def test_registered():
    assert "multica" in liveness.liveness_providers()
    assert isinstance(liveness.liveness_adapter_for("multica"), MulticaLivenessAdapter)


def test_live_probes_api_me_on_cloud_by_default(stack):
    result, calls = _check(stack)
    assert result.status == LIVE
    assert calls == [("GET", "https://api.multica.ai/api/me", {"Authorization": f"Bearer {SECRET}"})]
    assert result.detail == "GET https://api.multica.ai/api/me -> 200"


def test_self_hosted_url_from_tag(stack):
    result, calls = _check(stack, tags={"multica_url": "http://multica-backend-1:8080/"})
    assert result.status == LIVE
    assert calls[0][1] == "http://multica-backend-1:8080/api/me"


def test_whitespace_around_value_is_stripped(stack):
    _, calls = _check(stack, value=f"  {SECRET}\n")
    assert calls[0][2] == {"Authorization": f"Bearer {SECRET}"}


def test_401_is_dead(stack):
    result, _ = _check(stack, code=401)
    assert result.status == DEAD
    assert result.http_status == 401


@pytest.mark.parametrize("code", [403, 404, 429, 500, 503])
def test_other_failures_are_unknown(stack, code):
    # 403 is a temporarily disabled user or a CSRF failure, not a bad token.
    result, _ = _check(stack, code=code)
    assert result.status == UNKNOWN


@pytest.mark.parametrize("value", ["mat_run-token-0xBEEF", "eyJhbGciOiJIUzI1NiJ9.x.y", "mdt_x"])
def test_non_pat_value_not_sent(stack, value):
    result, calls = _check(stack, value=value)
    assert calls == []
    assert result.status == UNKNOWN
    assert "mul_" in result.detail


@pytest.mark.parametrize("url", ["ftp://multica.example", "multica.example", "https://",
                                 "https://user:pw@multica.example", "https://m.example?x=1"])
def test_bad_url_tag_not_probed(stack, url):
    result, calls = _check(stack, tags={"multica_url": url})
    assert calls == []
    assert result.status == UNKNOWN
    assert "multica_url" in result.detail


def test_liveness_provider_tag_selects_adapter(stack):
    stack["registry"].add(NAME, f"sm-{NAME}", provider="local",
                          tags={"liveness_provider": "multica"})
    assert liveness.provider_for(stack["registry"].get(NAME)) == "multica"
