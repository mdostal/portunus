"""Long-tail single-API-key SaaS liveness probes (PANT-1125, GH #204)."""
import json

import pytest

from portunus import liveness

SECRET = "sk-long-tail-do-not-leak-0xF00D"


class FakeTransport:
    def __init__(self, code=200):
        self.code = code
        self.calls = []

    def __call__(self, method, url, headers, timeout):
        self.calls.append((method, url, dict(headers)))
        return self.code


def _check(stack, provider, value=SECRET, code=200, tags=None, adapter=None):
    stack["registry"].add(f"{provider}-key", f"sm-{provider}-key", provider=provider, tags=tags)
    stack["backend"].set(f"sm-{provider}-key", value)
    transport = FakeTransport(code)
    if adapter is None:
        adapter = liveness.HTTPLivenessAdapter(
            provider, liveness.LONG_TAIL_PROBES[provider], transport=transport)
    else:
        adapter = adapter(transport=transport)
    result = liveness.check_reference(stack["registry"].get(f"{provider}-key"), stack["resolver"],
                                      adapter=adapter)
    assert value not in json.dumps(result.to_dict())
    return result, transport.calls


@pytest.mark.parametrize("provider", sorted(liveness.LONG_TAIL_PROBES) + ["tremendous"])
def test_long_tail_providers_registered(provider):
    assert provider in liveness.liveness_providers()
    assert liveness.liveness_adapter_for(provider).provider == provider


@pytest.mark.parametrize("provider,url,headers", [
    ("browserbase", "https://api.browserbase.com/v1/projects", {"X-BB-API-Key": SECRET}),
    ("firecrawl", "https://api.firecrawl.dev/v2/team/credit-usage",
     {"Authorization": f"Bearer {SECRET}"}),
    ("healthchecks", "https://healthchecks.io/api/v3/checks/", {"X-Api-Key": SECRET}),
    ("pexels", "https://api.pexels.com/v1/curated?per_page=1", {"Authorization": SECRET}),
    ("shippo", "https://api.goshippo.com/addresses?results=1",
     {"Authorization": f"ShippoToken {SECRET}"}),
    ("vercel-ai", "https://ai-gateway.vercel.sh/v1/credits",
     {"Authorization": f"Bearer {SECRET}"}),
])
def test_header_probes(stack, provider, url, headers):
    result, calls = _check(stack, provider)
    assert result.status == "live"
    assert calls == [("GET", url, headers)]
    assert result.detail == f"GET {url} -> 200"


@pytest.mark.parametrize("provider,url", [
    ("pixabay", f"https://pixabay.com/api/?per_page=3&key={SECRET}"),
    ("serpapi", f"https://serpapi.com/account.json?api_key={SECRET}"),
])
def test_query_param_probes_keep_key_out_of_headers_and_detail(stack, provider, url):
    result, calls = _check(stack, provider)
    assert calls == [("GET", url, {})]
    assert result.status == "live"
    assert SECRET not in result.detail


def test_query_param_value_is_url_encoded():
    probe = liveness.HTTPProbe(url="https://x.test/a", query_param="k")
    assert probe.url_for("a b&c") == "https://x.test/a?k=a+b%26c"
    assert probe.headers_for("a b&c") == {}


@pytest.mark.parametrize("provider", sorted(set(liveness.LONG_TAIL_PROBES) - {"pixabay"}))
def test_only_401_is_dead(stack, provider):
    probe = liveness.LONG_TAIL_PROBES[provider]
    assert probe.classify(401) == "dead"
    for code in (402, 403, 404, 429, 500):
        assert probe.classify(code) == "unknown"


def test_pixabay_invalid_key_400_is_dead(stack):
    result, _ = _check(stack, "pixabay", code=400)
    assert result.status == "dead"
    assert liveness.LONG_TAIL_PROBES["pixabay"].classify(429) == "unknown"


@pytest.mark.parametrize("value,tags,url", [
    ("TEST_abc", None, "https://testflight.tremendous.com/api/v2/funding_sources"),
    ("PROD_abc", None, "https://api.tremendous.com/api/v2/funding_sources"),
    ("legacy", {"tremendous_env": "sandbox"},
     "https://testflight.tremendous.com/api/v2/funding_sources"),
    ("PROD_abc", {"tremendous_env": "sandbox"},
     "https://api.tremendous.com/api/v2/funding_sources"),  # prefix wins
])
def test_tremendous_host_from_prefix_or_tag(stack, value, tags, url):
    result, calls = _check(stack, "tremendous", value=value, tags=tags,
                           adapter=liveness.TremendousLivenessAdapter)
    assert calls == [("GET", url, {"Authorization": f"Bearer {value}"})]
    assert result.status == "live"
    assert result.detail == f"GET {url} -> 200"


def test_tremendous_dead(stack):
    result, _ = _check(stack, "tremendous", value="PROD_abc", code=401,
                       adapter=liveness.TremendousLivenessAdapter)
    assert (result.status, result.http_status) == ("dead", 401)


def test_tremendous_without_env_is_unknown_and_not_probed(stack):
    result, calls = _check(stack, "tremendous", value="legacy-key",
                           adapter=liveness.TremendousLivenessAdapter)
    assert calls == []
    assert result.status == "unknown"
    assert "tremendous_env" in result.detail


def test_default_transport_sends_portunus_user_agent(monkeypatch):
    seen = {}

    class Resp:
        status = 200

        def __enter__(self):
            return self

        def __exit__(self, *exc):
            return False

    def fake_urlopen(req, timeout):
        seen.update(req.header_items())
        return Resp()

    monkeypatch.setattr(liveness.urllib.request, "urlopen", fake_urlopen)
    assert liveness._default_transport("GET", "https://x.test/", {"X-Api-Key": "v"}, 1.0) == 200
    assert seen["User-agent"] == "portunus-liveness"
    assert seen["X-api-key"] == "v"
