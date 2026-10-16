"""ResendRotationAdapter tests.

Every HTTP call goes through an injected fake transport -- the real Resend
API is never contacted. Tokens below are obviously fake.
"""
import json

import pytest

from portunus.audit import AuditChain
from portunus.registry import Registry
from portunus.rotation import (
    RotationAdapterError,
    RotationResult,
    ResendRotationAdapter,
    load_rotation_bindings,
    rotation_adapter_for,
)

ADMIN_TOKEN = "re_FAKE_ADMIN_TOKEN"
OLD_TOKEN = "re_FAKE_OLD_TOKEN"
NEW_TOKEN = "re_FAKE_NEW_TOKEN"
OLD_ID = "old-key-id-1111"
NEW_ID = "new-key-id-2222"


class _FakeResolver:
    """Stands in for Resolver: hands a fake admin key to the boundary only."""

    def __init__(self, registry=None):
        self.registry = registry
        self.templates = []

    def resolve_call(self, template, boundary):
        self.templates.append(template)
        return boundary(ADMIN_TOKEN)


def _make_transport(*, create_status=200, verify=(200, {}), delete_status=200):
    calls = []

    def transport(method, url, headers, body, timeout):
        calls.append({"method": method, "url": url, "headers": dict(headers), "body": body})
        if method == "POST" and url.endswith("/api-keys"):
            if create_status != 200:
                return create_status, {"name": "validation_error", "message": "API key is invalid"}
            return 200, {"id": NEW_ID, "object": "api_key", "token": NEW_TOKEN}
        if method == "GET":
            return verify
        if method == "DELETE":
            return delete_status, {"object": "api_key", "id": url.rsplit("/", 1)[1], "deleted": True}
        raise AssertionError(f"unexpected call {method} {url}")

    return transport, calls


class _FakeRef:
    def __init__(self, name="venues-prod-resend-api-key", tags=None):
        self.name = name
        self.sm_name = name
        self.project = ""
        self.tags = {"resend_api_key_id": OLD_ID} if tags is None else tags


@pytest.fixture
def local(home):
    from portunus.localvault import LocalEncryptedBackend
    backend = LocalEncryptedBackend()
    backend.store("venues-prod-resend-api-key", OLD_TOKEN)
    return backend


def _adapter(local, transport, **kw):
    return ResendRotationAdapter(transport=transport, local_backend=local, **kw)


def _methods(calls):
    return [(c["method"], c["url"].replace("https://api.resend.com", "")) for c in calls]


# --- registry / capability --------------------------------------------------

def test_capability_is_auto():
    assert ResendRotationAdapter().capability() == "auto"


def test_registered_under_resend():
    assert isinstance(rotation_adapter_for("resend"), ResendRotationAdapter)


# --- happy path, non-destructive default --------------------------------------

def test_default_creates_verifies_stores_and_does_not_delete(local):
    transport, calls = _make_transport()
    result = _adapter(local, transport).rotate(_FakeRef(), resolver=_FakeResolver())

    assert local.access("venues-prod-resend-api-key") == NEW_TOKEN
    assert _methods(calls) == [("POST", "/api-keys"), ("GET", "/api-keys?limit=1")]
    assert isinstance(result, RotationResult)
    assert (result.provider, result.key_id, result.phase, result.retired_old) == (
        "resend", NEW_ID, "stored", False)


def test_create_uses_admin_key_and_verify_uses_new_key(local):
    transport, calls = _make_transport()
    _adapter(local, transport).rotate(_FakeRef(), resolver=_FakeResolver())
    assert calls[0]["headers"]["Authorization"] == f"Bearer {ADMIN_TOKEN}"
    assert calls[1]["headers"]["Authorization"] == f"Bearer {NEW_TOKEN}"


def test_create_body_defaults_to_full_access_and_respects_name_limit(local):
    transport, calls = _make_transport()
    ref = _FakeRef(name="x" * 80, tags={})
    local.store(ref.sm_name, OLD_TOKEN)
    _adapter(local, transport).rotate(ref, resolver=_FakeResolver())
    body = calls[0]["body"]
    assert body["permission"] == "full_access"
    assert len(body["name"]) <= 50
    assert "domain_id" not in body


def test_sending_access_with_domain(local):
    transport, calls = _make_transport(
        verify=(401, {"name": "restricted_api_key",
                      "message": "This API key is restricted to only send emails."}))
    ref = _FakeRef(tags={"resend_permission": "sending_access", "resend_domain_id": "dom-1"})
    _adapter(local, transport).rotate(ref, resolver=_FakeResolver())
    assert calls[0]["body"]["permission"] == "sending_access"
    assert calls[0]["body"]["domain_id"] == "dom-1"
    # 401 restricted_api_key proves a valid sending-only key -> stored
    assert local.access("venues-prod-resend-api-key") == NEW_TOKEN


def test_invalid_permission_tag_raises_before_any_call(local):
    transport, calls = _make_transport()
    with pytest.raises(RotationAdapterError, match="resend_permission"):
        _adapter(local, transport).rotate(
            _FakeRef(tags={"resend_permission": "admin"}), resolver=_FakeResolver())
    assert calls == []


def test_admin_ref_defaults_to_self_and_tag_overrides(local):
    transport, _ = _make_transport()
    r = _FakeResolver()
    _adapter(local, transport).rotate(_FakeRef(), resolver=r)
    assert r.templates[0] == "{{secret:venues-prod-resend-api-key}}"

    r = _FakeResolver()
    _adapter(local, transport).rotate(
        _FakeRef(tags={"resend_admin_ref": "resend-admin"}), resolver=r)
    assert r.templates[0] == "{{secret:resend-admin}}"

    r = _FakeResolver()
    _adapter(local, transport, admin_credential_ref="{{secret:ctor-admin}}").rotate(
        _FakeRef(tags={"resend_admin_ref": "resend-admin"}), resolver=r)
    assert r.templates[0] == "{{secret:ctor-admin}}"


def test_no_resolver_raises(local):
    transport, calls = _make_transport()
    with pytest.raises(RotationAdapterError, match="resolver"):
        _adapter(local, transport).rotate(_FakeRef())
    assert calls == []


def test_default_records_old_key_as_superseded(local):
    transport, _ = _make_transport()
    _adapter(local, transport).rotate(_FakeRef(), resolver=_FakeResolver())
    assert load_rotation_bindings()["resend"].superseded_key_ids == (OLD_ID,)


def test_new_key_id_written_back_to_registry_tags(home, local):
    reg = Registry()
    reg.add("venues-prod-resend-api-key", "venues-prod-resend-api-key")
    reg.retag("venues-prod-resend-api-key", provider="resend",
              tags={"resend_api_key_id": OLD_ID, "resend_admin_ref": "resend-admin"})
    transport, _ = _make_transport()
    _adapter(local, transport).rotate(
        reg.get("venues-prod-resend-api-key"), resolver=_FakeResolver(registry=reg))
    tags = Registry().get("venues-prod-resend-api-key").tags
    assert tags == {"resend_api_key_id": NEW_ID, "resend_admin_ref": "resend-admin"}


# --- failures -----------------------------------------------------------------

def test_create_failure_stores_nothing(local):
    transport, calls = _make_transport(create_status=401)
    with pytest.raises(RotationAdapterError, match="HTTP 401 validation_error"):
        _adapter(local, transport).rotate(_FakeRef(), resolver=_FakeResolver())
    assert local.access("venues-prod-resend-api-key") == OLD_TOKEN
    assert _methods(calls) == [("POST", "/api-keys")]


@pytest.mark.parametrize("verify", [
    (400, {"name": "validation_error", "message": "API key is invalid"}),
    (401, {"name": "validation_error", "message": "API key is invalid"}),
    (403, {"name": "restricted_api_key", "message": "API key is not active"}),
    (403, {"name": "suspended_api_key", "message": "This API key is suspended"}),
    (500, {}),
])
def test_verify_failure_stores_nothing_and_cleans_up_new_key(local, verify):
    audit = AuditChain()
    transport, calls = _make_transport(verify=verify)
    with pytest.raises(RotationAdapterError, match="verification failed"):
        _adapter(local, transport, audit=audit).rotate(
            _FakeRef(), resolver=_FakeResolver(), retire_old=True)
    assert local.access("venues-prod-resend-api-key") == OLD_TOKEN
    # Only the never-stored NEW key is deleted; the old one is untouched.
    assert _methods(calls)[-1] == ("DELETE", f"/api-keys/{NEW_ID}")
    assert ("DELETE", f"/api-keys/{OLD_ID}") not in _methods(calls)
    assert any(e["result"] == "warn:verify-failed" for e in audit.entries())
    assert "resend" not in load_rotation_bindings()


def test_retire_old_without_key_id_refuses_before_creating(local):
    transport, calls = _make_transport()
    with pytest.raises(RotationAdapterError, match="resend_api_key_id"):
        _adapter(local, transport).rotate(_FakeRef(tags={}), resolver=_FakeResolver(),
                                          retire_old=True)
    assert calls == []


# --- retire_old ---------------------------------------------------------------

def test_retire_old_deletes_old_key_after_store(local):
    stored_at_delete = []
    transport, calls = _make_transport()

    def spying(method, url, headers, body, timeout):
        if method == "DELETE":
            stored_at_delete.append(local.access("venues-prod-resend-api-key"))
        return transport(method, url, headers, body, timeout)

    result = _adapter(local, spying).rotate(_FakeRef(), resolver=_FakeResolver(),
                                            retire_old=True)
    assert _methods(calls)[-1] == ("DELETE", f"/api-keys/{OLD_ID}")
    assert calls[-1]["headers"]["Authorization"] == f"Bearer {ADMIN_TOKEN}"
    assert stored_at_delete == [NEW_TOKEN]
    assert result.retired_old is True
    assert load_rotation_bindings()["resend"].superseded_key_ids == ()


def test_retire_old_404_counts_as_retired(local):
    transport, _ = _make_transport(delete_status=404)
    result = _adapter(local, transport).rotate(_FakeRef(), resolver=_FakeResolver(),
                                               retire_old=True)
    assert result.retired_old is True


def test_retire_old_delete_failure_keeps_new_key_and_superseded_record(local):
    transport, _ = _make_transport(delete_status=500)
    with pytest.raises(RotationAdapterError, match="new key IS stored"):
        _adapter(local, transport).rotate(_FakeRef(), resolver=_FakeResolver(),
                                          retire_old=True)
    assert local.access("venues-prod-resend-api-key") == NEW_TOKEN
    assert load_rotation_bindings()["resend"].superseded_key_ids == (OLD_ID,)


# --- secret boundary ------------------------------------------------------------

def test_no_token_in_result_audit_or_errors(home, local):
    audit = AuditChain()
    transport, _ = _make_transport()
    result = _adapter(local, transport, audit=audit).rotate(
        _FakeRef(), resolver=_FakeResolver())
    blob = json.dumps(result.__dict__) + json.dumps(list(audit.entries()))
    blob += (home / "rotation-bindings.json").read_text()
    for token in (ADMIN_TOKEN, NEW_TOKEN, OLD_TOKEN):
        assert token not in blob

    transport, _ = _make_transport(delete_status=500)
    with pytest.raises(RotationAdapterError) as exc:
        _adapter(local, transport).rotate(_FakeRef(), resolver=_FakeResolver(),
                                          retire_old=True)
    for token in (ADMIN_TOKEN, NEW_TOKEN, OLD_TOKEN):
        assert token not in str(exc.value)
