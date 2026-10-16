"""VercelRotationAdapter tests.

Every HTTP call goes through an injected fake transport -- the real Vercel
API is never contacted. Tokens below are obviously fake.
"""
import json
import time

import pytest

from portunus.audit import AuditChain
from portunus.registry import Registry
from portunus.rotation import (
    RotationAdapterError,
    RotationResult,
    VercelRotationAdapter,
    load_rotation_bindings,
    rotation_adapter_for,
)

REF = "vercel-deploy-token"
ADMIN_TOKEN = "vcp_FAKE_ADMIN_TOKEN"
OLD_TOKEN = "vcp_FAKE_OLD_TOKEN"
NEW_TOKEN = "vcp_FAKE_NEW_TOKEN"
OLD_ID = "old-token-id-1111"
NEW_ID = "new-token-id-2222"
TOKEN_IDS = {ADMIN_TOKEN: OLD_ID, OLD_TOKEN: OLD_ID, NEW_TOKEN: NEW_ID}


class _FakeResolver:
    """Stands in for Resolver: hands a fake token to the boundary only.
    The rotated ref itself resolves to OLD_TOKEN, anything else to the
    admin token -- unless `values` overrides it."""

    def __init__(self, registry=None, values=None):
        self.registry = registry
        self.templates = []
        self.values = values or {}

    def resolve_call(self, template, boundary):
        self.templates.append(template)
        default = OLD_TOKEN if template == "{{secret:%s}}" % REF else ADMIN_TOKEN
        return boundary(self.values.get(template, default))


def _make_transport(*, create_status=200, verify=None, delete_status=200, current_ok=True):
    calls = []

    def transport(method, url, headers, body, timeout):
        calls.append({"method": method, "url": url, "headers": dict(headers), "body": body})
        token = headers["Authorization"].removeprefix("Bearer ")
        if method == "POST" and "/v3/user/tokens" in url:
            if create_status != 200:
                return create_status, {"error": {"code": "forbidden",
                                                 "message": "Not authorized"}}
            return 200, {"token": {"id": NEW_ID, "name": body["name"], "type": "token"},
                         "bearerToken": NEW_TOKEN}
        if method == "GET" and url.endswith("/v5/user/tokens/current"):
            if token == NEW_TOKEN and verify is not None:
                return verify
            if token == OLD_TOKEN and not current_ok:
                return 403, {"error": {"code": "forbidden", "invalidToken": True}}
            return 200, {"token": {"id": TOKEN_IDS[token]}}
        if method == "DELETE":
            return delete_status, {"tokenId": url.rsplit("/", 1)[1]}
        raise AssertionError(f"unexpected call {method} {url}")

    return transport, calls


class _FakeRef:
    def __init__(self, name=REF, tags=None):
        self.name = name
        self.sm_name = name
        self.project = ""
        self.tags = {"vercel_token_id": OLD_ID} if tags is None else tags


@pytest.fixture
def local(home):
    from portunus.localvault import LocalEncryptedBackend
    backend = LocalEncryptedBackend()
    backend.store(REF, OLD_TOKEN)
    return backend


def _adapter(local, transport, **kw):
    return VercelRotationAdapter(transport=transport, local_backend=local, **kw)


def _methods(calls):
    return [(c["method"], c["url"].replace("https://api.vercel.com", "")) for c in calls]


# --- registry / capability --------------------------------------------------

def test_capability_is_auto():
    assert VercelRotationAdapter().capability() == "auto"


def test_registered_under_vercel():
    assert isinstance(rotation_adapter_for("vercel"), VercelRotationAdapter)


# --- happy path, non-destructive default --------------------------------------

def test_default_creates_verifies_stores_and_does_not_delete(local):
    transport, calls = _make_transport()
    result = _adapter(local, transport).rotate(_FakeRef(), resolver=_FakeResolver())

    assert local.access(REF) == NEW_TOKEN
    assert _methods(calls) == [("POST", "/v3/user/tokens"), ("GET", "/v5/user/tokens/current")]
    assert isinstance(result, RotationResult)
    assert (result.provider, result.key_id, result.phase, result.retired_old) == (
        "vercel", NEW_ID, "stored", False)


def test_create_uses_admin_token_and_verify_uses_new_token(local):
    transport, calls = _make_transport()
    _adapter(local, transport).rotate(
        _FakeRef(tags={"vercel_token_id": OLD_ID, "vercel_admin_ref": "vercel-admin"}),
        resolver=_FakeResolver())
    assert calls[0]["headers"]["Authorization"] == f"Bearer {ADMIN_TOKEN}"
    assert calls[1]["headers"]["Authorization"] == f"Bearer {NEW_TOKEN}"


def test_create_body_defaults(local):
    transport, calls = _make_transport()
    ref = _FakeRef(name="x" * 150, tags={"vercel_token_id": OLD_ID})
    _adapter(local, transport).rotate(ref, resolver=_FakeResolver())
    body = calls[0]["body"]
    assert len(body["name"]) <= 100
    assert body["name"].startswith("portunus-")
    assert "expiresAt" not in body and "projectId" not in body
    assert calls[0]["url"].endswith("/v3/user/tokens")


def test_ttl_sets_expires_at_in_milliseconds(local):
    transport, calls = _make_transport()
    before = time.time()
    _adapter(local, transport).rotate(
        _FakeRef(tags={"vercel_token_id": OLD_ID, "vercel_token_ttl_days": "30"}),
        resolver=_FakeResolver())
    expires = calls[0]["body"]["expiresAt"]
    assert isinstance(expires, int)
    assert (before + 30 * 86400) * 1000 - 5000 <= expires <= (time.time() + 30 * 86400) * 1000


@pytest.mark.parametrize("ttl", ["0", "-3", "soon"])
def test_bad_ttl_raises_before_any_call(local, ttl):
    transport, calls = _make_transport()
    with pytest.raises(RotationAdapterError, match="vercel_token_ttl_days"):
        _adapter(local, transport).rotate(
            _FakeRef(tags={"vercel_token_ttl_days": ttl}), resolver=_FakeResolver())
    assert calls == []


def test_team_and_project_scoping(local):
    transport, calls = _make_transport()
    _adapter(local, transport).rotate(
        _FakeRef(tags={"vercel_token_id": OLD_ID, "vercel_team_id": "team_abc",
                       "vercel_team_slug": "ignored", "vercel_project_id": "prj_1"}),
        resolver=_FakeResolver())
    assert calls[0]["url"].endswith("/v3/user/tokens?teamId=team_abc")
    assert calls[0]["body"]["projectId"] == "prj_1"

    transport, calls = _make_transport()
    _adapter(local, transport).rotate(
        _FakeRef(tags={"vercel_token_id": OLD_ID, "vercel_team_slug": "my team"}),
        resolver=_FakeResolver())
    assert calls[0]["url"].endswith("/v3/user/tokens?slug=my%20team")


def test_admin_ref_defaults_to_self_and_tag_overrides(local):
    transport, _ = _make_transport()
    r = _FakeResolver()
    _adapter(local, transport).rotate(_FakeRef(), resolver=r)
    assert r.templates[0] == "{{secret:%s}}" % REF

    r = _FakeResolver()
    _adapter(local, transport).rotate(
        _FakeRef(tags={"vercel_token_id": OLD_ID, "vercel_admin_ref": "vercel-admin"}),
        resolver=r)
    assert r.templates[0] == "{{secret:vercel-admin}}"

    r = _FakeResolver()
    _adapter(local, transport, admin_credential_ref="{{secret:ctor-admin}}").rotate(
        _FakeRef(tags={"vercel_token_id": OLD_ID, "vercel_admin_ref": "vercel-admin"}),
        resolver=r)
    assert r.templates[0] == "{{secret:ctor-admin}}"


def test_no_resolver_raises(local):
    transport, calls = _make_transport()
    with pytest.raises(RotationAdapterError, match="resolver"):
        _adapter(local, transport).rotate(_FakeRef())
    assert calls == []


def test_default_records_old_token_as_superseded(local):
    transport, _ = _make_transport()
    _adapter(local, transport).rotate(_FakeRef(), resolver=_FakeResolver())
    assert load_rotation_bindings()["vercel"].superseded_key_ids == (OLD_ID,)


def test_new_token_id_written_back_to_registry_tags(home, local):
    reg = Registry()
    reg.add(REF, REF)
    reg.retag(REF, provider="vercel",
              tags={"vercel_token_id": OLD_ID, "vercel_admin_ref": "vercel-admin"})
    transport, _ = _make_transport()
    _adapter(local, transport).rotate(reg.get(REF), resolver=_FakeResolver(registry=reg))
    tags = Registry().get(REF).tags
    assert tags == {"vercel_token_id": NEW_ID, "vercel_admin_ref": "vercel-admin"}


# --- old token id discovery ---------------------------------------------------

def test_missing_token_id_is_looked_up_with_the_stored_token(local):
    transport, calls = _make_transport()
    r = _FakeResolver()
    result = _adapter(local, transport).rotate(_FakeRef(tags={}), resolver=r, retire_old=True)
    assert _methods(calls)[0] == ("GET", "/v5/user/tokens/current")
    assert calls[0]["headers"]["Authorization"] == f"Bearer {OLD_TOKEN}"
    assert _methods(calls)[-1] == ("DELETE", f"/v3/user/tokens/{OLD_ID}")
    assert result.retired_old is True


def test_retire_old_with_unresolvable_token_id_refuses_before_creating(local):
    transport, calls = _make_transport(current_ok=False)
    with pytest.raises(RotationAdapterError, match="vercel_token_id"):
        _adapter(local, transport).rotate(_FakeRef(tags={}), resolver=_FakeResolver(),
                                          retire_old=True)
    assert all(c["method"] == "GET" for c in calls)


def test_unknown_old_id_without_retire_still_rotates(local):
    transport, calls = _make_transport(current_ok=False)
    _adapter(local, transport).rotate(
        _FakeRef(tags={"vercel_admin_ref": "vercel-admin"}), resolver=_FakeResolver())
    assert local.access(REF) == NEW_TOKEN
    assert "vercel" not in load_rotation_bindings()


# --- failures -----------------------------------------------------------------

def test_create_failure_stores_nothing(local):
    transport, calls = _make_transport(create_status=403)
    with pytest.raises(RotationAdapterError, match="HTTP 403 forbidden: Not authorized"):
        _adapter(local, transport).rotate(_FakeRef(), resolver=_FakeResolver())
    assert local.access(REF) == OLD_TOKEN
    assert _methods(calls) == [("POST", "/v3/user/tokens")]


@pytest.mark.parametrize("verify", [
    (403, {"error": {"code": "forbidden", "invalidToken": True}}),
    (200, {"token": {"id": "someone-else"}}),
    (200, {}),
    (500, {}),
])
def test_verify_failure_stores_nothing_and_cleans_up_new_token(local, verify):
    audit = AuditChain()
    transport, calls = _make_transport(verify=verify)
    with pytest.raises(RotationAdapterError, match="verification failed"):
        _adapter(local, transport, audit=audit).rotate(
            _FakeRef(), resolver=_FakeResolver(), retire_old=True)
    assert local.access(REF) == OLD_TOKEN
    # Only the never-stored NEW token is deleted; the old one is untouched.
    assert _methods(calls)[-1] == ("DELETE", f"/v3/user/tokens/{NEW_ID}")
    assert ("DELETE", f"/v3/user/tokens/{OLD_ID}") not in _methods(calls)
    assert any(e["result"] == "warn:verify-failed" for e in audit.entries())
    assert "vercel" not in load_rotation_bindings()


# --- retire_old ---------------------------------------------------------------

def test_retire_old_deletes_old_token_after_store(local):
    stored_at_delete = []
    transport, calls = _make_transport()

    def spying(method, url, headers, body, timeout):
        if method == "DELETE":
            stored_at_delete.append(local.access(REF))
        return transport(method, url, headers, body, timeout)

    result = _adapter(local, spying).rotate(
        _FakeRef(tags={"vercel_token_id": OLD_ID, "vercel_admin_ref": "vercel-admin"}),
        resolver=_FakeResolver(), retire_old=True)
    assert _methods(calls)[-1] == ("DELETE", f"/v3/user/tokens/{OLD_ID}")
    assert calls[-1]["headers"]["Authorization"] == f"Bearer {ADMIN_TOKEN}"
    assert stored_at_delete == [NEW_TOKEN]
    assert result.retired_old is True
    assert load_rotation_bindings()["vercel"].superseded_key_ids == ()


def test_retire_old_404_counts_as_retired(local):
    transport, _ = _make_transport(delete_status=404)
    result = _adapter(local, transport).rotate(_FakeRef(), resolver=_FakeResolver(),
                                               retire_old=True)
    assert result.retired_old is True


def test_retire_old_delete_failure_keeps_new_token_and_superseded_record(local):
    transport, _ = _make_transport(delete_status=500)
    with pytest.raises(RotationAdapterError, match="new token IS stored"):
        _adapter(local, transport).rotate(_FakeRef(), resolver=_FakeResolver(),
                                          retire_old=True)
    assert local.access(REF) == NEW_TOKEN
    assert load_rotation_bindings()["vercel"].superseded_key_ids == (OLD_ID,)


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

    for kw in ({"delete_status": 500}, {"verify": (403, {})}, {"create_status": 401}):
        transport, _ = _make_transport(**kw)
        with pytest.raises(RotationAdapterError) as exc:
            _adapter(local, transport).rotate(_FakeRef(), resolver=_FakeResolver(),
                                              retire_old=True)
        for token in (ADMIN_TOKEN, NEW_TOKEN, OLD_TOKEN):
            assert token not in str(exc.value)
