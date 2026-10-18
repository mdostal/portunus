"""Vercel token liveness probe (PANT-1149, GH #144)."""
import io
import json
import urllib.error

import pytest

from portunus import liveness

SECRET = "vcp-deploy-token-do-not-leak-0xFACE"
URL = "https://api.vercel.com/v5/user/tokens/current"
NOW = 1_800_000_000.0  # fixed clock, epoch seconds
DAY = 86400


class FakeTransport:
    def __init__(self, code=200, facts=None):
        self.code = code
        self.facts = facts if facts is not None else {"invalid_token": False, "expires_at": None}
        self.calls = []

    def __call__(self, method, url, headers, timeout):
        self.calls.append((method, url, dict(headers)))
        return self.code, self.facts


def _check(stack, code=200, facts=None, value=SECRET, provider="vercel", tags=None):
    stack["registry"].add("vercel-deploy-token", "vercel-deploy-token", provider=provider, tags=tags)
    stack["backend"].set("vercel-deploy-token", value)
    transport = FakeTransport(code, facts)
    adapter = liveness.VercelLivenessAdapter(transport=transport, now=lambda: NOW)
    result = liveness.check_reference(stack["registry"].get("vercel-deploy-token"), stack["resolver"],
                                      adapter=adapter)
    assert value.strip() not in json.dumps(result.to_dict())
    return result, transport.calls


def test_registered():
    assert "vercel" in liveness.liveness_providers()
    assert isinstance(liveness.liveness_adapter_for("vercel"), liveness.VercelLivenessAdapter)


def test_live_sends_token_only_as_bearer(stack):
    result, calls = _check(stack, value=SECRET + "\n")
    assert calls == [("GET", URL, {"Authorization": f"Bearer {SECRET}"})]
    assert (result.status, result.http_status) == ("live", 200)
    assert result.detail == f"GET {URL} -> 200"


def test_403_invalid_token_is_dead(stack):
    # The exact response the dead `vercel-deploy-token` returned on 2026-09-13.
    result, _ = _check(stack, code=403, facts={"invalid_token": True, "expires_at": None})
    assert (result.status, result.http_status) == ("dead", 403)
    assert "invalidToken" in result.detail


def test_403_without_invalid_token_is_unknown(stack):
    # Vercel also answers 403 for a missing token or a scope problem.
    result, _ = _check(stack, code=403, facts={"invalid_token": False, "expires_at": None})
    assert result.status == "unknown"


@pytest.mark.parametrize("code,expected", [(401, "dead"), (429, "unknown"), (500, "unknown"),
                                           (404, "unknown")])
def test_other_statuses(stack, code, expected):
    result, _ = _check(stack, code=code)
    assert result.status == expected


def test_provider_tag_routes_gcp_ref_to_vercel(stack):
    stack["registry"].add("deploy", "sm-deploy", provider="gcp", tags={"liveness_provider": "vercel"})
    ref = stack["registry"].get("deploy")
    assert liveness.provider_for(ref) == "vercel"
    assert ref in liveness.checkable_references(stack["registry"])


def test_far_expiry_reported(stack):
    expires_ms = int((NOW + 90 * DAY) * 1000)
    result, _ = _check(stack, facts={"invalid_token": False, "expires_at": expires_ms})
    assert result.status == "live"
    assert "expires 2027-04-15 (in 90d)" in result.detail
    assert "rotate" not in result.detail


def test_near_expiry_warns_with_rotate_command(stack):
    expires_ms = int((NOW + 3 * DAY + 60) * 1000)
    result, _ = _check(stack, facts={"invalid_token": False, "expires_at": expires_ms})
    assert result.status == "live"
    assert "EXPIRES" in result.detail and "(in 3d)" in result.detail
    assert "portunus rotation run vercel-deploy-token" in result.detail


# --- body handling: only two facts ever leave the transport ---------------

def test_facts_extracts_only_flag_and_expiry():
    body = json.dumps({"token": {"id": "tok_1", "name": SECRET, "expiresAt": 1234}}).encode()
    assert liveness._vercel_facts(body) == {"invalid_token": False, "expires_at": 1234}
    body = b'{"error":{"code":"forbidden","message":"Not authorized","invalidToken":true}}'
    assert liveness._vercel_facts(body) == {"invalid_token": True, "expires_at": None}


@pytest.mark.parametrize("raw", [b"", b"not json", b"[]", b'{"error":"x","token":5}',
                                 b'{"error":{"invalidToken":"true"},"token":{"expiresAt":true}}'])
def test_facts_tolerates_garbage(raw):
    assert liveness._vercel_facts(raw) == {"invalid_token": False, "expires_at": None}


def test_default_transport_error_body_reduced_to_facts(monkeypatch):
    body = io.BytesIO(b'{"error":{"code":"forbidden","message":"' + SECRET.encode()
                      + b'","invalidToken":true}}')
    err = urllib.error.HTTPError(URL, 403, "Forbidden", {}, body)

    def fake_urlopen(req, timeout):
        raise err

    monkeypatch.setattr(liveness.urllib.request, "urlopen", fake_urlopen)
    code, facts = liveness._default_vercel_transport("GET", URL, {}, 1.0)
    assert (code, facts) == (403, {"invalid_token": True, "expires_at": None})
    assert SECRET not in repr(facts)


def test_default_transport_network_error_has_no_detail_leak(monkeypatch):
    def fake_urlopen(req, timeout):
        raise urllib.error.URLError(TimeoutError(f"boom {SECRET}"))

    monkeypatch.setattr(liveness.urllib.request, "urlopen", fake_urlopen)
    with pytest.raises(liveness.LivenessError) as info:
        liveness._default_vercel_transport("GET", URL, {}, 1.0)
    assert SECRET not in str(info.value)


def test_network_failure_is_unknown(stack):
    def boom(method, url, headers, timeout):
        raise liveness.LivenessError("request failed: TimeoutError")

    stack["registry"].add("v", "sm-v", provider="vercel")
    stack["backend"].set("sm-v", SECRET)
    result = liveness.check_reference(stack["registry"].get("v"), stack["resolver"],
                                      adapter=liveness.VercelLivenessAdapter(transport=boom))
    assert result.status == "unknown"
