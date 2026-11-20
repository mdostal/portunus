"""Stripe API-key liveness probe (PANT-1134, GH #195).

Secret and restricted keys are probed with `GET /v1/balance`. The key type
is read from the prefix inside the resolver boundary and named in the
result's detail -- never anything after the prefix. Publishable keys,
webhook secrets, organization keys and unrecognised values read unknown
without a request, never a false dead."""
import json

import pytest

from portunus import liveness
from portunus.liveness import (
    DEAD, LIVE, UNKNOWN, StripeLivenessAdapter, check_reference, checkable_references,
    stripe_key_type,
)

URL = "https://api.stripe.com/v1/balance"
TAIL = "do-not-leak-0xC0FFEE"


class FakeTransport:
    def __init__(self, code=200):
        self.code = code
        self.calls = []

    def __call__(self, method, url, headers, timeout):
        self.calls.append((method, url, dict(headers)))
        return self.code


def _check(stack, value, name="stripe-prod-secret-key", code=200, provider="stripe", tags=None):
    stack["registry"].add(name, f"sm-{name}", provider=provider, tags=tags)
    stack["backend"].set(f"sm-{name}", value)
    transport = FakeTransport(code)
    result = check_reference(stack["registry"].get(name), stack["resolver"],
                             adapter=StripeLivenessAdapter(transport=transport))
    dumped = json.dumps(result.to_dict()) + repr(result)
    assert value.strip() not in dumped
    assert TAIL not in dumped
    return result, transport.calls


def test_registered():
    assert "stripe" in liveness.liveness_providers()
    assert isinstance(liveness.liveness_adapter_for("stripe"), StripeLivenessAdapter)


@pytest.mark.parametrize("value,expected", [
    ("sk_live_" + TAIL, ("secret key", "live")),
    ("sk_test_" + TAIL, ("secret key", "test")),
    ("rk_live_" + TAIL, ("restricted key", "live")),
    ("rk_test_" + TAIL, ("restricted key", "test")),
    ("pk_live_" + TAIL, ("publishable key", "live")),
    ("pk_test_" + TAIL, ("publishable key", "test")),
    ("sk_org_live_" + TAIL, ("organization secret key", "live")),
    ("whsec_" + TAIL, ("webhook signing secret", "")),
    ("  rk_live_" + TAIL + "\n", ("restricted key", "live")),
    ("xyz_" + TAIL, ("", "")),
    ("", ("", "")),
])
def test_stripe_key_type_from_prefix_only(value, expected):
    assert stripe_key_type(value) == expected


@pytest.mark.parametrize("prefix,label", [
    ("sk_live_", "live secret key"),
    ("rk_test_", "test restricted key"),
])
def test_secret_and_restricted_keys_probe_balance_as_bearer(stack, prefix, label):
    value = prefix + TAIL
    # GCP Secret Manager values often carry a trailing newline.
    result, calls = _check(stack, value + "\n")
    assert calls == [("GET", URL, {"Authorization": f"Bearer {value}"})]
    assert (result.status, result.http_status) == (LIVE, 200)
    assert result.detail == f"{label}: GET {URL} -> 200"


@pytest.mark.parametrize("prefix", ["sk_live_", "rk_live_"])
def test_401_is_dead(stack, prefix):
    result, _ = _check(stack, prefix + TAIL, code=401)
    assert (result.status, result.http_status) == (DEAD, 401)


def test_restricted_key_without_balance_read_is_live(stack):
    result, _ = _check(stack, "rk_live_" + TAIL, code=403)
    assert (result.status, result.http_status) == (LIVE, 403)
    assert "no Balance read permission" in result.detail


def test_secret_key_403_is_unknown(stack):
    result, _ = _check(stack, "sk_live_" + TAIL, code=403)
    assert result.status == UNKNOWN


@pytest.mark.parametrize("code", [429, 500, 503])
def test_outage_is_unknown_not_dead(stack, code):
    result, _ = _check(stack, "sk_live_" + TAIL, code=code)
    assert result.status == UNKNOWN


@pytest.mark.parametrize("value,label", [
    ("pk_live_" + TAIL, "live publishable key"),
    ("whsec_" + TAIL, "webhook signing secret"),
    ("sk_org_live_" + TAIL, "live organization secret key"),
    ("not-a-stripe-value-" + TAIL, "unrecognised prefix"),
])
def test_non_api_values_are_not_sent(stack, value, label):
    result, calls = _check(stack, value)
    assert calls == []
    assert result.status == UNKNOWN
    assert result.http_status is None
    assert result.detail.startswith(label)
    assert "not probed" in result.detail


def test_liveness_provider_tag_routes_gcp_ref_to_stripe(stack):
    stack["registry"].add("game-library-dev-stripe-secret-key", "sm-gl", provider="gcp",
                          tags={"liveness_provider": "stripe"})
    stack["backend"].set("sm-gl", "sk_test_" + TAIL)
    result = check_reference(stack["registry"].get("game-library-dev-stripe-secret-key"),
                             stack["resolver"])
    assert result.provider == "stripe"


def test_sweep_skips_publishable_and_webhook_names(stack):
    for name in ("stripe-prod-secret-key", "stripe-prod-publishable-key",
                 "event-api-prod-stripe-webhook-secret", "venues-dev-secret-stripe-api-key"):
        stack["registry"].add(name, f"sm-{name}", provider="stripe")
    names = sorted(r.name for r in checkable_references(stack["registry"]))
    assert names == ["stripe-prod-secret-key", "venues-dev-secret-stripe-api-key"]
