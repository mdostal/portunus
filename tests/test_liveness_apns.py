"""APNs auth-key liveness probe (PANT-1127, GH #202)."""
import base64
import json

import pytest
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import ec, rsa
from cryptography.hazmat.primitives.asymmetric.utils import encode_dss_signature

from portunus import liveness
from portunus.liveness import ApnsLivenessAdapter, apns_provider_token, check_reference

KEY = ec.generate_private_key(ec.SECP256R1())
P8 = KEY.private_bytes(serialization.Encoding.PEM, serialization.PrivateFormat.PKCS8,
                       serialization.NoEncryption()).decode()
TEAM = {"apns_team_id": "TEAM123456"}


class FakeApns:
    def __init__(self, code=400, reason="BadDeviceToken"):
        self.code, self.reason = code, reason
        self.calls = []

    def __call__(self, host, path, headers, body, timeout):
        self.calls.append((host, path, dict(headers), body))
        return self.code, self.reason


def _check(stack, name="apns-authkey-B9FVCQ4GB4", value=P8, tags=TEAM, transport=None):
    stack["registry"].add(name, f"sm-{name}", provider="apns", tags=tags)
    stack["backend"].set(f"sm-{name}", value)
    transport = transport or FakeApns()
    adapter = ApnsLivenessAdapter(transport=transport, clock=lambda: 1_700_000_000)
    result = check_reference(stack["registry"].get(name), stack["resolver"], adapter=adapter)
    blob = json.dumps(result.to_dict())
    assert value.strip() not in blob
    for _, _, headers, _ in transport.calls:
        assert headers["authorization"].split()[1] not in blob
    return result, transport.calls


def _b64url_decode(part):
    return base64.urlsafe_b64decode(part + "=" * (-len(part) % 4))


def test_registered():
    assert "apns" in liveness.liveness_providers()
    assert liveness.liveness_adapter_for("apns").provider == "apns"


def test_provider_token_is_a_valid_es256_jwt():
    token = apns_provider_token(P8, "B9FVCQ4GB4", "TEAM123456", 1_700_000_000)
    header, claims, sig = token.split(".")
    assert json.loads(_b64url_decode(header)) == {"alg": "ES256", "kid": "B9FVCQ4GB4"}
    assert json.loads(_b64url_decode(claims)) == {"iss": "TEAM123456", "iat": 1_700_000_000}
    raw = _b64url_decode(sig)
    assert len(raw) == 64  # JOSE r||s, not DER
    der = encode_dss_signature(int.from_bytes(raw[:32], "big"), int.from_bytes(raw[32:], "big"))
    KEY.public_key().verify(der, f"{header}.{claims}".encode(), ec.ECDSA(hashes.SHA256()))


def test_base64_wrapped_p8_is_accepted():
    wrapped = base64.b64encode(P8.encode()).decode()
    assert apns_provider_token(wrapped, "B9FVCQ4GB4", "T", 1) is not None


@pytest.mark.parametrize("value", [
    "not a key",
    "-----BEGIN PRIVATE KEY-----\nAAAA\n-----END PRIVATE KEY-----\n",
    rsa.generate_private_key(public_exponent=65537, key_size=2048).private_bytes(
        serialization.Encoding.PEM, serialization.PrivateFormat.PKCS8,
        serialization.NoEncryption()).decode(),
])
def test_non_p8_values_yield_no_token(value):
    assert apns_provider_token(value, "B9FVCQ4GB4", "T", 1) is None


def test_undeliverable_silent_push_to_sandbox_by_default(stack):
    result, calls = _check(stack)
    assert result.status == "live"
    (host, path, headers, body), = calls
    assert host == "api.sandbox.push.apple.com"
    assert path == "/3/device/" + "0" * 64
    assert headers["apns-push-type"] == "background"
    assert headers["apns-expiration"] == "0"
    assert headers["apns-priority"] == "5"
    assert "apns-topic" not in headers
    assert headers["authorization"].startswith("bearer ")
    assert json.loads(body) == {"aps": {"content-available": 1}}
    assert result.http_status == 400
    assert "BadDeviceToken" in result.detail and "B9FVCQ4GB4" in result.detail


def test_production_env_and_topic_tags(stack):
    tags = {**TEAM, "apns_env": "production", "apns_topic": "com.example.app"}
    _, calls = _check(stack, tags=tags)
    host, _, headers, _ = calls[0]
    assert host == "api.push.apple.com"
    assert headers["apns-topic"] == "com.example.app"


def test_key_id_tag_overrides_name(stack):
    tags = {**TEAM, "apns_key_id": "ZZZZZ99999"}
    _, calls = _check(stack, name="shindig-apns-authkey-p8", tags=tags)
    token = calls[0][2]["authorization"].split()[1]
    assert json.loads(_b64url_decode(token.split(".")[0]))["kid"] == "ZZZZZ99999"


def test_invalid_provider_token_is_dead(stack):
    result, _ = _check(stack, transport=FakeApns(403, "InvalidProviderToken"))
    assert (result.status, result.http_status) == ("dead", 403)


@pytest.mark.parametrize("code,reason", [
    (403, "BadEnvironmentKeyIdInToken"),  # key scoped to the other environment
    (403, "UnrelatedKeyIdInToken"),       # topic-specific key, other app
    (403, "ExpiredProviderToken"),        # local clock skew
    (403, "(unrecognised)"),
    (403, ""),
    (429, "TooManyProviderTokenUpdates"),
    (500, "InternalServerError"),
    (503, "ServiceUnavailable"),
])
def test_everything_else_is_unknown(stack, code, reason):
    result, _ = _check(stack, transport=FakeApns(code, reason))
    assert result.status == "unknown"


@pytest.mark.parametrize("name,tags,missing", [
    ("apns-authkey-B9FVCQ4GB4", {}, "apns_team_id"),
    ("apns-authkey-unknown-jan9", TEAM, "apns_key_id"),
    ("shindig-apns-authkey-p8", TEAM, "apns_key_id"),
    ("apns-authkey-B9FVCQ4GB4", {**TEAM, "apns_env": "staging"}, "apns_env"),
])
def test_missing_config_is_unknown_without_a_request(stack, name, tags, missing):
    result, calls = _check(stack, name=name, tags=tags)
    assert calls == []
    assert result.status == "unknown"
    assert missing in result.detail
    assert not ApnsLivenessAdapter().can_probe(stack["registry"].get(name))


def test_sweep_skips_unconfigured_keys(stack):
    reg = stack["registry"]
    reg.add("apns-authkey-B9FVCQ4GB4", "sm-a", provider="apns", tags=TEAM)
    reg.add("apns-authkey-unknown-jan9", "sm-b", provider="apns", tags=TEAM)
    reg.add("shindig-apns-authkey-p8", "sm-c", provider="gcp", tags={"liveness_provider": "apns"})
    names = [r.name for r in liveness.checkable_references(reg)]
    assert names == ["apns-authkey-B9FVCQ4GB4"]


def test_value_that_is_not_a_key_is_unknown_without_a_request(stack):
    result, calls = _check(stack, value="definitely-not-a-p8-0xBAD")
    assert calls == []
    assert result.status == "unknown"
    assert ".p8" in result.detail


def test_network_failure_is_unknown(stack):
    def boom(*a):
        raise liveness.LivenessError("request failed: TimeoutError")

    stack["registry"].add("apns-authkey-B9FVCQ4GB4", "sm-a", provider="apns", tags=TEAM)
    stack["backend"].set("sm-a", P8)
    result = check_reference(stack["registry"].get("apns-authkey-B9FVCQ4GB4"), stack["resolver"],
                             adapter=ApnsLivenessAdapter(transport=boom))
    assert (result.status, result.detail) == ("unknown", "request failed: TimeoutError")


@pytest.mark.parametrize("body,reason", [
    (b'{"reason":"BadDeviceToken"}', "BadDeviceToken"),
    (b'{"reason":"InvalidProviderToken"}', "InvalidProviderToken"),
    (b'{"reason":"sk-echoed-secret"}', "(unrecognised)"),
    (b"<html>gateway</html>", ""),
    (b"", ""),
    (b"[1]", ""),
])
def test_only_allowlisted_reasons_leave_the_body(body, reason):
    assert liveness._apns_reason(body) == reason


def test_missing_h2_is_a_liveness_error(monkeypatch):
    import builtins
    real_import = builtins.__import__

    def fake_import(name, *args, **kwargs):
        if name == "h2" or name.startswith("h2."):
            raise ImportError(name)
        return real_import(name, *args, **kwargs)

    monkeypatch.setattr(builtins, "__import__", fake_import)
    with pytest.raises(liveness.LivenessError, match=r"pantheon-portunus\[apns\]"):
        liveness._apns_default_transport("api.sandbox.push.apple.com", "/", {}, b"", 1.0)
