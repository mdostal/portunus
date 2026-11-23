"""Autumn secret key liveness probe (PANT-1150, GH #143)."""
import io
import json
import urllib.error

import pytest

from portunus import liveness
from portunus.liveness import DEAD, LIVE, UNKNOWN, AutumnLivenessAdapter, check_reference

LIVE_KEY = "am_sk_live_autumn-do-not-leak-0xBEEF"
TEST_KEY = "am_sk_test_autumn-do-not-leak-0xCAFE"
NAME = "social-engine-prod-autumn-secret-key"
URL = "https://api.useautumn.com/v1/plans.list"


class FakeTransport:
    def __init__(self, code=200, err=""):
        self.code = code
        self.err = err
        self.calls = []

    def __call__(self, method, url, headers, body, timeout):
        self.calls.append((method, url, dict(headers), body))
        return self.code, self.err


def _check(stack, value=LIVE_KEY, code=200, err="", name=NAME, provider="autumn", tags=None):
    stack["registry"].add(name, f"sm-{name}", provider=provider, tags=tags)
    stack["backend"].set(f"sm-{name}", value)
    transport = FakeTransport(code, err)
    result = check_reference(stack["registry"].get(name), stack["resolver"],
                             adapter=AutumnLivenessAdapter(transport=transport))
    dumped = json.dumps(result.to_dict())
    assert value.strip() not in dumped
    assert value.strip() not in repr(result)
    # Nothing after the prefix ever leaves the boundary.
    assert "do-not-leak" not in dumped
    return result, transport.calls


def test_registered_and_swept(stack):
    assert "autumn" in liveness.liveness_providers()
    assert isinstance(liveness.liveness_adapter_for("autumn"), AutumnLivenessAdapter)
    stack["registry"].add(NAME, f"sm-{NAME}", provider="autumn")
    assert [r.name for r in liveness.checkable_references(stack["registry"])] == [NAME]


def test_gcp_key_picked_up_via_liveness_provider_tag(stack):
    # The incident key is registered under provider "gcp".
    result, calls = _check(stack, provider="gcp", tags={"liveness_provider": "autumn"})
    assert result.provider == "autumn"
    assert len(calls) == 1


def test_sweeps_skip_publishable_key_and_webhook_secret_names(stack):
    for name in ("flayr-autumn-publishable-key", "flayr-autumn-webhook-secret"):
        stack["registry"].add(name, f"sm-{name}", provider="autumn")
    stack["registry"].add(NAME, f"sm-{NAME}", provider="autumn")
    assert [r.name for r in liveness.checkable_references(stack["registry"])] == [NAME]


def test_live_key_sent_as_bearer_to_plans_list(stack):
    result, calls = _check(stack, value=f"  {LIVE_KEY}\n")
    assert calls == [("POST", URL, {"Authorization": f"Bearer {LIVE_KEY}",
                                    "Content-Type": "application/json"}, b"{}")]
    assert (result.status, result.http_status) == (LIVE, 200)
    assert result.detail == f"live secret key: POST {URL} -> 200"


def test_sandbox_key_named_in_detail(stack):
    result, calls = _check(stack, value=TEST_KEY)
    assert calls[0][2]["Authorization"] == f"Bearer {TEST_KEY}"
    assert result.detail.startswith("sandbox secret key:")


def test_401_invalid_secret_key_is_dead(stack):
    # The response the dead `social-engine-prod-autumn-secret-key` got on 2026-09-13.
    result, _ = _check(stack, code=401, err="invalid_secret_key")
    assert (result.status, result.http_status) == (DEAD, 401)
    assert "invalid_secret_key" in result.detail
    assert "Autumn dashboard" in result.detail


@pytest.mark.parametrize("err", ["invalid_request", "no_secret_key", ""])
def test_other_401s_are_unknown(stack, err):
    # Autumn uses 401 for a malformed header and a missing key too.
    result, _ = _check(stack, code=401, err=err)
    assert result.status == UNKNOWN
    assert "not proof the key is dead" in result.detail


@pytest.mark.parametrize("code", [400, 403, 404, 429, 500, 503])
def test_other_statuses_are_unknown(stack, code):
    result, _ = _check(stack, code=code)
    assert (result.status, result.http_status) == (UNKNOWN, code)


@pytest.mark.parametrize("value,label", [
    ("am_pk_live_autumn-do-not-leak-0x1", "publishable key"),
    ("whsec_autumn-do-not-leak-0x2", "webhook signing secret"),
    ("sk_live_autumn-do-not-leak-0x3", "unrecognised prefix"),
])
def test_non_secret_values_are_not_sent(stack, value, label):
    result, calls = _check(stack, value=value)
    assert calls == []
    assert result.status == UNKNOWN
    assert result.http_status is None
    assert result.detail.startswith(label)
    assert "not probed" in result.detail


def test_network_failure_is_unknown(stack):
    def boom(*_args):
        raise liveness.LivenessError("request failed: URLError")

    stack["registry"].add(NAME, f"sm-{NAME}", provider="autumn")
    stack["backend"].set(f"sm-{NAME}", LIVE_KEY)
    result = check_reference(stack["registry"].get(NAME), stack["resolver"],
                             adapter=AutumnLivenessAdapter(transport=boom))
    assert result.status == UNKNOWN
    assert LIVE_KEY not in result.detail


def test_dead_result_drives_escalation_clock(stack, home):
    result, _ = _check(stack, code=401, err="invalid_secret_key")
    statuses = liveness.record_results([result])
    assert [st.ref_name for st in liveness.failing(statuses)] == [NAME]
    assert statuses[NAME].first_failed_at == result.checked_at


# --- default transport: the body is reduced to a known error code -----------

def _http_error(code, body: bytes):
    return urllib.error.HTTPError(URL, code, "err", {}, io.BytesIO(body))


def _run_transport(monkeypatch, exc=None, status=200):
    seen = {}

    class Resp:
        def __init__(self):
            self.status = status

        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

        def read(self, *a):  # pragma: no cover - must never be called
            raise AssertionError("2xx body must not be read")

    def fake_urlopen(req, timeout):
        seen["req"] = req
        if exc is not None:
            raise exc
        return Resp()

    monkeypatch.setattr(liveness.urllib.request, "urlopen", fake_urlopen)
    out = liveness._default_autumn_transport("POST", URL, {"Authorization": "Bearer x"}, b"{}", 1.0)
    return out, seen["req"]


def test_default_transport_extracts_only_known_code(monkeypatch):
    # Autumn's real body echoes the start of the key back in `message`.
    body = (b'{"message":"Invalid secret key: ' + LIVE_KEY[:15].encode()
            + b'****","code":"invalid_secret_key","env":"sandbox"}')
    (code, err), req = _run_transport(monkeypatch, exc=_http_error(401, body))
    assert (code, err) == (401, "invalid_secret_key")
    assert req.get_method() == "POST" and req.data == b"{}"
    assert req.get_header("User-agent") == "portunus-liveness"


def test_default_transport_drops_unknown_code(monkeypatch):
    body = json.dumps({"code": LIVE_KEY, "message": LIVE_KEY}).encode()
    (code, err), _ = _run_transport(monkeypatch, exc=_http_error(401, body))
    assert (code, err) == (401, "")


@pytest.mark.parametrize("body", [b"", b"not json", b"[1,2]", b'{"code": 7}'])
def test_default_transport_tolerates_odd_bodies(monkeypatch, body):
    (code, err), _ = _run_transport(monkeypatch, exc=_http_error(401, body))
    assert (code, err) == (401, "")


def test_default_transport_2xx_body_never_read(monkeypatch):
    (code, err), _ = _run_transport(monkeypatch, status=200)
    assert (code, err) == (200, "")


def test_default_transport_network_error_has_no_detail_leak(monkeypatch):
    def fake_urlopen(req, timeout):
        raise urllib.error.URLError(TimeoutError(f"boom {LIVE_KEY}"))

    monkeypatch.setattr(liveness.urllib.request, "urlopen", fake_urlopen)
    with pytest.raises(liveness.LivenessError) as info:
        liveness._default_autumn_transport("POST", URL, {}, b"{}", 1.0)
    assert LIVE_KEY not in str(info.value)
