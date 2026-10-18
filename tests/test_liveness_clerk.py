"""Clerk secret-key liveness probe (PANT-1148, GH #145).

Only the instance secret key authenticates Clerk's Backend API. The
publishable key and the webhook signing secret must read unknown without a
request, never a false dead."""
import json

import pytest

from portunus import liveness
from portunus.liveness import DEAD, LIVE, UNKNOWN, ClerkLivenessAdapter, check_reference

SECRET = "sk_test_clerk-do-not-leak-0xBEEF"
URL = "https://api.clerk.com/v1/jwks"


class FakeTransport:
    def __init__(self, code=200):
        self.code = code
        self.calls = []

    def __call__(self, method, url, headers, timeout):
        self.calls.append((method, url, dict(headers)))
        return self.code


def _check(stack, name="social-engine-prod-clerk-secret-key", value=SECRET, code=200,
           provider="clerk", tags=None):
    stack["registry"].add(name, f"sm-{name}", provider=provider, tags=tags)
    stack["backend"].set(f"sm-{name}", value)
    transport = FakeTransport(code)
    result = check_reference(stack["registry"].get(name), stack["resolver"],
                             adapter=ClerkLivenessAdapter(transport=transport))
    assert value.strip() not in json.dumps(result.to_dict())
    assert value.strip() not in repr(result)
    return result, transport.calls


def test_registered():
    assert "clerk" in liveness.liveness_providers()
    assert isinstance(liveness.liveness_adapter_for("clerk"), ClerkLivenessAdapter)


def test_secret_key_sent_only_as_bearer_to_jwks(stack):
    # GCP Secret Manager values often carry a trailing newline.
    result, calls = _check(stack, value=SECRET + "\n")
    assert calls == [("GET", URL, {"Authorization": f"Bearer {SECRET}"})]
    assert (result.status, result.http_status) == (LIVE, 200)
    assert result.detail == f"GET {URL} -> 200"


def test_401_clerk_key_invalid_is_dead(stack):
    # What api.clerk.com returned for a bogus sk_test_ key on 2026-10-18.
    result, _ = _check(stack, code=401)
    assert (result.status, result.http_status) == (DEAD, 401)


@pytest.mark.parametrize("code", [403, 404, 429, 500, 503])
def test_other_statuses_are_unknown(stack, code):
    result, _ = _check(stack, code=code)
    assert result.status == UNKNOWN


@pytest.mark.parametrize("name,reason", [
    ("social-engine-prod-clerk-publishable-key", "public"),
    ("social-engine-prod-clerk-webhook-secret", "webhook signing secret"),
])
def test_non_secret_kinds_are_not_probed(stack, name, reason):
    result, calls = _check(stack, name=name, value="pk_test_or_whsec_value")
    assert calls == []
    assert result.status == UNKNOWN
    assert reason in result.detail


def test_unrecognised_kind_is_not_probed(stack):
    result, calls = _check(stack, name="clerk-thing")
    assert calls == []
    assert result.status == UNKNOWN
    assert "liveness_kind" in result.detail


@pytest.mark.parametrize("value", ["pk_live_Y2xlcmsuZXhhbXBsZS5jb20k", "whsec_abcdef0123456789"])
def test_mislabelled_value_is_not_sent(stack, value):
    # A publishable key or webhook secret stored under a secret-key name
    # would get 401 and read as a false dead.
    result, calls = _check(stack, value=value)
    assert calls == []
    assert result.status == UNKNOWN
    assert "not a Clerk secret key" in result.detail


def test_kind_tag_overrides_name(stack):
    result, calls = _check(stack, name="clerk-backend", tags={"liveness_kind": "secret-key"})
    assert result.status == LIVE
    assert len(calls) == 1


def test_gcp_ref_routed_by_provider_tag_and_swept(stack):
    tags = {"liveness_provider": "clerk"}
    for name in ("social-engine-dev-clerk-secret-key", "social-engine-dev-clerk-publishable-key",
                 "social-engine-dev-clerk-webhook-secret"):
        stack["registry"].add(name, f"sm-{name}", provider="gcp", tags=tags)
    swept = [r.name for r in liveness.checkable_references(stack["registry"])]
    assert swept == ["social-engine-dev-clerk-secret-key"]


def test_secret_never_reaches_audit(stack):
    stack["registry"].add("social-engine-prod-clerk-secret-key", "sm-ck", provider="clerk")
    stack["backend"].set("sm-ck", SECRET)
    ref = stack["registry"].get("social-engine-prod-clerk-secret-key")
    result = check_reference(ref, stack["resolver"], adapter=ClerkLivenessAdapter(transport=FakeTransport(401)))
    liveness.audit_liveness(stack["audit"], ref, result)
    liveness.record_results([result])
    assert SECRET not in stack["audit"].path.read_text()
    assert SECRET not in liveness._status_path().read_text()
