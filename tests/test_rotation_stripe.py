"""StripeRotationAdapter tests (PANT-1409).

Every HTTP call goes through an injected fake transport -- the real Stripe
API is never contacted. Keys below are obviously fake. The fake "Stripe"
encrypts the new secret to the public key the adapter sends, exactly as the
v2 IAM API does (compact JWE, RSA-OAEP-256 + A256GCM), so the decrypt path
is exercised for real.
"""
import base64
import json
import os

import pytest
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import padding
from cryptography.hazmat.primitives.ciphers.aead import AESGCM

from portunus.audit import AuditChain
from portunus.registry import Registry
from portunus.rotation import (
    RotationAdapterError,
    RotationResult,
    load_rotation_bindings,
    rotation_adapter_for,
)
from portunus.stripe_rotation import (
    STRIPE_PREVIEW_VERSION,
    StripeRotationAdapter,
    classify_stripe_key,
    decrypt_jwe_compact,
)

REF = "shop-stripe-secret"
ADMIN_KEY = "sk_live_FAKE_ADMIN"
OLD_KEY = "sk_live_FAKE_OLD"
NEW_KEY = "sk_live_FAKE_NEW_ROTATED"
OLD_PK = "pk_live_FAKE_OLD"
NEW_PK = "pk_live_FAKE_NEW"
OLD_ID = "ak_old_1111"
NEW_ID = "ak_new_2222"


def _b64u(data: bytes) -> str:
    return base64.urlsafe_b64encode(data).rstrip(b"=").decode()


def _jwe_encrypt(plaintext: str, public_pem: str, alg="RSA-OAEP-256", enc="A256GCM") -> str:
    pub = serialization.load_pem_public_key(public_pem.encode())
    header = _b64u(json.dumps({"alg": alg, "enc": enc}).encode())
    cek = os.urandom({"A128GCM": 16, "A256GCM": 32}[enc])
    h = hashes.SHA256() if alg == "RSA-OAEP-256" else hashes.SHA1()
    wrapped = pub.encrypt(cek, padding.OAEP(mgf=padding.MGF1(h), algorithm=h, label=None))
    iv = os.urandom(12)
    sealed = AESGCM(cek).encrypt(iv, plaintext.encode(), header.encode())
    return ".".join([header, _b64u(wrapped), _b64u(iv), _b64u(sealed[:-16]), _b64u(sealed[-16:])])


class _FakeResolver:
    """Hands a fake key to the boundary only. The rotated ref resolves to
    `ref_value`; any other template to the admin key."""

    def __init__(self, registry=None, ref_value=OLD_KEY, ref_name=REF):
        self.registry = registry
        self.ref_value = ref_value
        self.ref_name = ref_name
        self.templates = []

    def resolve_call(self, template, boundary):
        self.templates.append(template)
        value = self.ref_value if template == "{{secret:%s}}" % self.ref_name else ADMIN_KEY
        return boundary(value)


def _make_transport(*, rotate_status=200, verify_status="active", balance=(200, {}),
                    expire_status=200, new_secret=NEW_KEY, plaintext=False, key_type="secret_key"):
    calls = []

    def transport(method, url, headers, body, timeout):
        path = url.replace("https://api.stripe.com", "")
        calls.append({"method": method, "path": path, "headers": dict(headers), "body": body})
        if method == "POST" and path.endswith("/rotate"):
            if rotate_status != 200:
                return rotate_status, {"error": {"type": "invalid_request_error",
                                                 "code": "resource_missing",
                                                 "message": "No such api key"}}
            resp = {"id": NEW_ID, "object": "v2.iam.api_key", "type": key_type,
                    "status": "active", "livemode": True}
            if key_type == "publishable_key":
                resp["publishable_key"] = {"token": NEW_PK}
            elif plaintext:
                resp["secret_key"] = {"token": new_secret}
            else:
                pem = body["public_key"]["pem_key"]["data"]
                resp["secret_key"] = {"encrypted_secret": {
                    "ciphertext": _jwe_encrypt(new_secret, pem), "format": "jwe_compact"},
                    "secret_token_redacted": "sk_live_...TED"}
            return 200, resp
        if method == "GET" and path == f"/v2/iam/api_keys/{NEW_ID}":
            resp = {"id": NEW_ID, "type": key_type, "status": verify_status}
            if key_type == "publishable_key":
                resp["publishable_key"] = {"token": NEW_PK}
            return 200, resp
        if method == "GET" and path == "/v1/balance":
            return balance
        if method == "POST" and path.endswith("/expire"):
            return expire_status, {"id": path.split("/")[-2], "status": "expired"}
        raise AssertionError(f"unexpected call {method} {path}")

    return transport, calls


class _FakeRef:
    def __init__(self, name=REF, tags=None):
        self.name = name
        self.sm_name = name
        self.tags = {"stripe_key_id": OLD_ID, "stripe_key_type": "secret_key"} if tags is None else tags


@pytest.fixture
def local(home):
    from portunus.localvault import LocalEncryptedBackend
    backend = LocalEncryptedBackend()
    backend.store(REF, OLD_KEY)
    return backend


def _adapter(local, transport, **kw):
    return StripeRotationAdapter(transport=transport, local_backend=local, **kw)


def _methods(calls):
    return [(c["method"], c["path"]) for c in calls]


# --- registry / capability ---------------------------------------------------

def test_capability_is_auto():
    assert StripeRotationAdapter().capability() == "auto"


def test_registered_under_stripe():
    assert isinstance(rotation_adapter_for("stripe"), StripeRotationAdapter)


# --- happy path, non-destructive default ---------------------------------------

def test_default_rotates_with_grace_verifies_stores_and_does_not_expire(local):
    transport, calls = _make_transport()
    result = _adapter(local, transport).rotate(_FakeRef(), resolver=_FakeResolver())

    assert local.access(REF) == NEW_KEY
    assert _methods(calls) == [
        ("POST", f"/v2/iam/api_keys/{OLD_ID}/rotate"),
        ("GET", f"/v2/iam/api_keys/{NEW_ID}"),
        ("GET", "/v1/balance"),
    ]
    assert calls[0]["body"]["expire_current_key_in_minutes"] == 10080
    assert isinstance(result, RotationResult)
    assert (result.provider, result.key_id, result.phase, result.retired_old) == (
        "stripe", NEW_ID, "stored", False)
    assert load_rotation_bindings()["stripe"].superseded_key_ids == (OLD_ID,)


def test_rotate_sends_rsa_public_key_and_preview_headers(local):
    transport, calls = _make_transport()
    _adapter(local, transport).rotate(_FakeRef(), resolver=_FakeResolver())
    rotate = calls[0]
    pem = rotate["body"]["public_key"]["pem_key"]
    assert pem["algorithm"] == "RSA"
    assert pem["data"].startswith("-----BEGIN PUBLIC KEY-----")
    assert serialization.load_pem_public_key(pem["data"].encode()).key_size >= 2048
    assert rotate["headers"]["Authorization"] == f"Bearer {OLD_KEY}"  # ref rotates itself
    assert rotate["headers"]["Stripe-Version"] == STRIPE_PREVIEW_VERSION
    assert "Stripe-Context" not in rotate["headers"]
    assert calls[2]["headers"]["Authorization"] == f"Bearer {NEW_KEY}"  # verify uses NEW key


def test_admin_ref_context_version_and_grace_tags(local):
    transport, calls = _make_transport()
    tags = {"stripe_key_id": OLD_ID, "stripe_key_type": "secret_key",
            "stripe_admin_ref": "stripe-admin", "stripe_context": "acct_123",
            "stripe_api_version": "2027-01-01.preview", "stripe_grace_minutes": "60"}
    _adapter(local, transport).rotate(_FakeRef(tags=tags), resolver=_FakeResolver())
    h = calls[0]["headers"]
    assert h["Authorization"] == f"Bearer {ADMIN_KEY}"
    assert h["Stripe-Context"] == "acct_123"
    assert h["Stripe-Version"] == "2027-01-01.preview"
    assert calls[0]["body"]["expire_current_key_in_minutes"] == 60


def test_plaintext_testmode_secret_is_accepted(local):
    transport, _ = _make_transport(plaintext=True, new_secret="sk_test_FAKE_NEW")
    _adapter(local, transport).rotate(_FakeRef(), resolver=_FakeResolver())
    assert local.access(REF) == "sk_test_FAKE_NEW"


def test_restricted_key_passes_verify_on_permission_error(local):
    transport, _ = _make_transport(
        new_secret="rk_live_FAKE_NEW",
        balance=(403, {"error": {"type": "permission_error", "message": "needs rak_balance_read"}}))
    _adapter(local, transport).rotate(_FakeRef(), resolver=_FakeResolver())
    assert local.access(REF) == "rk_live_FAKE_NEW"


def test_key_type_classified_from_stored_value_when_untagged(local):
    transport, calls = _make_transport()
    resolver = _FakeResolver()
    _adapter(local, transport).rotate(_FakeRef(tags={"stripe_key_id": OLD_ID}), resolver=resolver)
    assert local.access(REF) == NEW_KEY
    assert "public_key" in calls[0]["body"]


# --- publishable keys ------------------------------------------------------------

def test_publishable_key_rotation_needs_no_public_key(home):
    from portunus.localvault import LocalEncryptedBackend
    local = LocalEncryptedBackend()
    local.store("shop-stripe-pk", OLD_PK)
    transport, calls = _make_transport(key_type="publishable_key")
    ref = _FakeRef(name="shop-stripe-pk",
                   tags={"stripe_key_id": OLD_ID, "stripe_admin_ref": "shop-stripe-secret"})
    _adapter(local, transport).rotate(
        ref, resolver=_FakeResolver(ref_value=OLD_PK, ref_name="shop-stripe-pk"))
    assert local.access("shop-stripe-pk") == NEW_PK
    assert "public_key" not in calls[0]["body"]
    assert calls[0]["headers"]["Authorization"] == f"Bearer {ADMIN_KEY}"
    assert ("GET", "/v1/balance") not in _methods(calls)


def test_publishable_key_without_admin_ref_refuses_before_any_call(home):
    transport, calls = _make_transport(key_type="publishable_key")
    ref = _FakeRef(name="pk", tags={"stripe_key_id": OLD_ID, "stripe_key_type": "publishable_key"})
    with pytest.raises(RotationAdapterError, match="stripe_admin_ref"):
        StripeRotationAdapter(transport=transport).rotate(ref, resolver=_FakeResolver())
    assert calls == []


# --- retire (opt-in) --------------------------------------------------------------

def test_retire_old_expires_old_key_after_store(local):
    transport, calls = _make_transport()
    result = _adapter(local, transport).rotate(_FakeRef(), resolver=_FakeResolver(), retire_old=True)
    assert _methods(calls)[-1] == ("POST", f"/v2/iam/api_keys/{OLD_ID}/expire")
    # Still rotated with a grace window -- the old key dies only via /expire,
    # after the new one is verified and stored.
    assert calls[0]["body"]["expire_current_key_in_minutes"] == 10080
    assert result.retired_old is True
    assert local.access(REF) == NEW_KEY
    assert load_rotation_bindings()["stripe"].superseded_key_ids == ()


def test_retire_failure_keeps_new_key_and_superseded_entry(local):
    transport, _ = _make_transport(expire_status=500)
    with pytest.raises(RotationAdapterError, match="new key IS stored"):
        _adapter(local, transport).rotate(_FakeRef(), resolver=_FakeResolver(), retire_old=True)
    assert local.access(REF) == NEW_KEY
    assert load_rotation_bindings()["stripe"].superseded_key_ids == (OLD_ID,)


def test_retire_404_counts_as_retired(local):
    transport, _ = _make_transport(expire_status=404)
    result = _adapter(local, transport).rotate(_FakeRef(), resolver=_FakeResolver(), retire_old=True)
    assert result.retired_old is True


# --- failures leave the old reference untouched ----------------------------------

def test_missing_key_id_refuses_before_any_call(local):
    transport, calls = _make_transport()
    with pytest.raises(RotationAdapterError, match="stripe_key_id"):
        _adapter(local, transport).rotate(_FakeRef(tags={}), resolver=_FakeResolver())
    assert calls == []
    assert local.access(REF) == OLD_KEY


def test_rotate_rejected_leaves_old_value_and_hints_preview(local):
    transport, _ = _make_transport(rotate_status=404)
    with pytest.raises(RotationAdapterError) as exc:
        _adapter(local, transport).rotate(_FakeRef(), resolver=_FakeResolver())
    msg = str(exc.value)
    assert "HTTP 404" in msg and "preview" in msg and "old key left intact" in msg
    assert local.access(REF) == OLD_KEY


@pytest.mark.parametrize("kw", [
    {"verify_status": "expired"},
    {"balance": (401, {"error": {"type": "invalid_request_error"}})},
])
def test_verify_failure_stores_nothing_and_expires_new_key(local, kw):
    transport, calls = _make_transport(**kw)
    audit = AuditChain()
    with pytest.raises(RotationAdapterError, match="verification failed"):
        _adapter(local, transport, audit=audit).rotate(_FakeRef(), resolver=_FakeResolver())
    assert local.access(REF) == OLD_KEY
    assert _methods(calls)[-1] == ("POST", f"/v2/iam/api_keys/{NEW_ID}/expire")
    assert ("POST", f"/v2/iam/api_keys/{OLD_ID}/expire") not in _methods(calls)
    assert any(e["result"].startswith("warn:verify-failed") for e in audit.entries())


def test_undecryptable_secret_stores_nothing(local):
    transport, _ = _make_transport()

    def bad_transport(method, url, headers, body, timeout):
        status, resp = transport(method, url, headers, body, timeout)
        if url.endswith("/rotate"):
            resp["secret_key"]["encrypted_secret"]["ciphertext"] = "a.b.c.d.e"
        return status, resp

    with pytest.raises(RotationAdapterError):
        _adapter(local, bad_transport).rotate(_FakeRef(), resolver=_FakeResolver())
    assert local.access(REF) == OLD_KEY


@pytest.mark.parametrize("grace", ["0", "10081", "soon"])
def test_bad_grace_rejected_before_any_call(local, grace):
    transport, calls = _make_transport()
    tags = {"stripe_key_id": OLD_ID, "stripe_key_type": "secret_key", "stripe_grace_minutes": grace}
    with pytest.raises(RotationAdapterError, match="stripe_grace_minutes"):
        _adapter(local, transport).rotate(_FakeRef(tags=tags), resolver=_FakeResolver())
    assert calls == []


def test_unclassifiable_value_rejected_before_any_call(local):
    transport, calls = _make_transport()
    with pytest.raises(RotationAdapterError, match="stripe_key_type"):
        _adapter(local, transport).rotate(
            _FakeRef(tags={"stripe_key_id": OLD_ID}), resolver=_FakeResolver(ref_value="whsec_x"))
    assert calls == []


# --- secrets never leak -------------------------------------------------------------

def test_no_key_material_in_errors_result_or_audit(local):
    audit = AuditChain()
    transport, _ = _make_transport(expire_status=500)
    with pytest.raises(RotationAdapterError) as exc:
        _adapter(local, transport, audit=audit).rotate(
            _FakeRef(), resolver=_FakeResolver(), retire_old=True)
    blob = str(exc.value) + json.dumps(audit.entries())
    for secret in (ADMIN_KEY, OLD_KEY, NEW_KEY):
        assert secret not in blob


def test_success_audit_entry(local):
    audit = AuditChain()
    transport, _ = _make_transport()
    _adapter(local, transport, audit=audit).rotate(_FakeRef(), resolver=_FakeResolver())
    last = audit.entries()[-1]
    assert last["action"] == "rotate:stripe-key"
    assert last["result"] == f"ok:new={NEW_ID}:old-expires-in=10080m"
    assert NEW_KEY not in json.dumps(audit.entries())


# --- registry bookkeeping -------------------------------------------------------------

def test_registry_records_new_id_clears_request_and_unlocks(local):
    reg = Registry()
    reg.add(REF, REF, provider="stripe", state="locked",
            tags={"stripe_key_id": OLD_ID, "stripe_key_type": "secret_key",
                  "rotation_requested": "true", "status": "chat-origin-rotate-after-testing"})
    transport, _ = _make_transport()
    _adapter(local, transport).rotate(reg.require(REF), resolver=_FakeResolver(registry=reg))
    ref = Registry().require(REF)
    assert ref.tags["stripe_key_id"] == NEW_ID
    assert "rotation_requested" not in ref.tags
    assert ref.state == "enabled"


def test_failed_rotation_leaves_registry_untouched(local):
    reg = Registry()
    reg.add(REF, REF, provider="stripe", state="locked",
            tags={"stripe_key_id": OLD_ID, "stripe_key_type": "secret_key"})
    transport, _ = _make_transport(rotate_status=404)
    with pytest.raises(RotationAdapterError):
        _adapter(local, transport).rotate(reg.require(REF), resolver=_FakeResolver(registry=reg))
    ref = Registry().require(REF)
    assert ref.tags["stripe_key_id"] == OLD_ID
    assert ref.state == "locked"


# --- helpers ---------------------------------------------------------------------------

def test_classify_stripe_key():
    assert classify_stripe_key("pk_live_x") == "publishable_key"
    assert classify_stripe_key("sk_test_x") == "secret_key"
    assert classify_stripe_key("rk_live_x") == "secret_key"
    assert classify_stripe_key("whsec_x") == ""


@pytest.mark.parametrize("alg,enc", [("RSA-OAEP-256", "A256GCM"), ("RSA-OAEP", "A128GCM")])
def test_decrypt_jwe_round_trip(alg, enc):
    from portunus.stripe_rotation import _ephemeral_rsa_keypair
    priv, pem = _ephemeral_rsa_keypair()
    assert decrypt_jwe_compact(_jwe_encrypt("sk_live_ROUNDTRIP", pem, alg, enc), priv) == "sk_live_ROUNDTRIP"


def test_decrypt_jwe_with_wrong_key_fails_without_leaking():
    from portunus.stripe_rotation import _ephemeral_rsa_keypair
    _, pem = _ephemeral_rsa_keypair()
    other, _ = _ephemeral_rsa_keypair()
    token = _jwe_encrypt("sk_live_SECRET_VALUE", pem)
    with pytest.raises(RotationAdapterError) as exc:
        decrypt_jwe_compact(token, other)
    assert "SECRET_VALUE" not in str(exc.value)
