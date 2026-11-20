"""UpstashRotationAdapter tests (PANT-1129).

Every Upstash Developer API and REST call goes through an injected fake
transport -- nothing real is contacted. Credentials below are obviously fake."""
import base64
import json

import pytest

from portunus.audit import AuditChain
from portunus.rotation import (
    RotationAdapterError,
    RotationResult,
    UpstashRotationAdapter,
    _upstash_rest_url,
    rotation_adapter_for,
)

REF = "orchestration-dev-upstash-redis-rest-token"
RO_REF = "orchestration-dev-upstash-redis-rest-readonly-token"
API_KEY = "FAKE-upstash-mgmt-key"
EMAIL = "ops@example.invalid"
OLD_TOKEN = "FAKE_OLD_rest_token_AAAA"
NEW_TOKEN = "FAKE_NEW_rest_token_BBBB"
NEW_RO = "FAKE_NEW_ro_token_CCCC"
NEW_PW = "FAKE_NEW_password_DDDD"
DB = "96ad0856-fake-4ee7-9666-e81abd0349e1"
API = "https://api.upstash.com/v2"
REST = "https://beloved-stallion-58500.upstash.io"
TAGS = {"upstash_database_id": DB, "upstash_api_key_ref": "upstash-api-key",
        "upstash_email_ref": "orchestration-dev-upstash-email"}
BASIC = "Basic " + base64.b64encode(f"{EMAIL}:{API_KEY}".encode()).decode()


class _FakeResolver:
    def __init__(self, values=None):
        self.templates = []
        self.values = {"{{secret:%s}}" % REF: OLD_TOKEN,
                       "{{secret:upstash-api-key}}": API_KEY,
                       "{{secret:orchestration-dev-upstash-email}}": EMAIL,
                       **(values or {})}

    def resolve_call(self, template, boundary):
        self.templates.append(template)
        return boundary(self.values[template])


class _FakeRef:
    def __init__(self, tags=None, backend=""):
        self.name = REF
        self.sm_name = REF
        self.backend = backend
        self.tags = dict(TAGS) if tags is None else tags


def _db(token, ro="FAKE_OLD_ro", password="FAKE_OLD_pw", endpoint="beloved-stallion-58500"):
    body = {"database_id": DB, "endpoint": endpoint, "state": "active"}
    for k, v in (("rest_token", token), ("read_only_rest_token", ro), ("password", password)):
        if v is not None:
            body[k] = v
    return body


def _transport(*, get=None, reset=None, pings=((200, {"result": "PONG"}),)):
    calls = []
    pings = list(pings)

    def transport(method, url, headers, body, timeout):
        calls.append((method, url, headers["Authorization"]))
        if url == f"{API}/redis/database/{DB}" and method == "GET":
            return get if get is not None else (200, _db(OLD_TOKEN))
        if url == f"{API}/redis/reset-password/{DB}" and method == "POST":
            return reset if reset is not None else (200, _db(NEW_TOKEN, NEW_RO, NEW_PW))
        if url.endswith("/ping") and method == "GET":
            p = pings.pop(0) if len(pings) > 1 else pings[0]
            if isinstance(p, Exception):
                raise p
            return p
        raise AssertionError(f"unexpected call {method} {url}")

    return transport, calls


@pytest.fixture
def local(home):
    from portunus.localvault import LocalEncryptedBackend
    backend = LocalEncryptedBackend()
    backend.store(REF, OLD_TOKEN)
    backend.store(RO_REF, "FAKE_OLD_ro")
    return backend


def _adapter(local, transport, **kw):
    return UpstashRotationAdapter(transport=transport, local_backend=local,
                                  sleep=lambda s: None, **kw)


def _resets(calls):
    return [c for c in calls if "reset-password" in c[1]]


# --- registry / capability --------------------------------------------------

def test_registered_and_auto():
    adapter = rotation_adapter_for("upstash")
    assert isinstance(adapter, UpstashRotationAdapter) and adapter.capability() == "auto"


@pytest.mark.parametrize("endpoint,url", [
    ("beloved-stallion-58500", REST),
    ("beloved-stallion-58500.upstash.io", REST),
    ("https://beloved-stallion-58500.upstash.io/", REST),
    ("", ""),
    ("http://evil.example", ""),
    ("user@host.example", ""),
])
def test_rest_url_from_endpoint(endpoint, url):
    assert _upstash_rest_url(endpoint) == url


# --- happy path ---------------------------------------------------------------

def test_check_reset_store_verify(local):
    transport, calls = _transport()
    result = _adapter(local, transport).rotate(_FakeRef(), _FakeResolver(), retire_old=True)

    assert local.access(REF) == NEW_TOKEN
    assert local.access(RO_REF) == "FAKE_OLD_ro"  # untagged: left alone
    assert [c[:2] for c in calls] == [
        ("GET", f"{API}/redis/database/{DB}"),
        ("POST", f"{API}/redis/reset-password/{DB}"),
        ("GET", f"{REST}/ping"),
    ]
    assert calls[0][2] == calls[1][2] == BASIC
    assert calls[2][2] == f"Bearer {NEW_TOKEN}"
    assert isinstance(result, RotationResult)
    assert (result.provider, result.key_id, result.phase, result.retired_old) == (
        "upstash", DB, "verified", True)
    assert NEW_TOKEN not in json.dumps(result.__dict__)


def test_literal_email_tag(local):
    transport, calls = _transport()
    tags = {**TAGS, "upstash_email": EMAIL}
    del tags["upstash_email_ref"]
    resolver = _FakeResolver()
    _adapter(local, transport).rotate(_FakeRef(tags), resolver, retire_old=True)
    assert calls[0][2] == BASIC
    assert "{{secret:orchestration-dev-upstash-email}}" not in resolver.templates


def test_read_only_token_ref_is_updated(local):
    transport, _ = _transport()
    tags = {**TAGS, "upstash_read_only_token_ref": RO_REF}
    _adapter(local, transport).rotate(_FakeRef(tags), _FakeResolver(), retire_old=True)
    assert local.access(REF) == NEW_TOKEN and local.access(RO_REF) == NEW_RO


def test_rest_url_ref_overrides_endpoint(local):
    transport, calls = _transport()
    tags = {**TAGS, "upstash_rest_url_ref": "upstash-rest-url"}
    resolver = _FakeResolver({"{{secret:upstash-rest-url}}": "https://custom.upstash.io\n"})
    _adapter(local, transport).rotate(_FakeRef(tags), resolver, retire_old=True)
    assert calls[-1][1] == "https://custom.upstash.io/ping"


def test_verify_retries_while_database_restarts(local):
    transport, calls = _transport(pings=[RotationAdapterError("down"), (503, {}),
                                         (200, {"result": "PONG"})])
    _adapter(local, transport).rotate(_FakeRef(), _FakeResolver(), retire_old=True)
    assert sum(c[1].endswith("/ping") for c in calls) == 3


def test_audit_records_ok_without_secrets(local):
    audit = AuditChain()
    transport, _ = _transport()
    _adapter(local, transport, audit=audit).rotate(_FakeRef(), _FakeResolver(), retire_old=True)
    raw = audit.path.read_text()
    assert "rotate:upstash-rest-token" in raw and f"ok:database={DB}" in raw
    for secret in (NEW_TOKEN, OLD_TOKEN, NEW_RO, NEW_PW, API_KEY):
        assert secret not in raw


# --- refusals before anything is reset ---------------------------------------

def test_requires_retire_old_ack(local):
    transport, calls = _transport()
    with pytest.raises(RotationAdapterError, match="--retire-old"):
        _adapter(local, transport).rotate(_FakeRef(), _FakeResolver())
    assert calls == [] and local.access(REF) == OLD_TOKEN


@pytest.mark.parametrize("missing", ["upstash_database_id", "upstash_api_key_ref",
                                     "upstash_email_ref"])
def test_requires_tags(local, missing):
    tags = {k: v for k, v in TAGS.items() if k != missing}
    with pytest.raises(RotationAdapterError, match=missing.replace("_ref", "")):
        _adapter(local, _transport()[0]).rotate(_FakeRef(tags), _FakeResolver(), retire_old=True)


def test_refuses_non_local_backend(local):
    with pytest.raises(RotationAdapterError, match="local vault"):
        _adapter(local, _transport()[0]).rotate(
            _FakeRef(backend="gcp"), _FakeResolver(), retire_old=True)


def test_bare_name_and_missing_resolver_refused(local):
    adapter = _adapter(local, _transport()[0])
    with pytest.raises(RotationAdapterError, match="registry reference"):
        adapter.rotate(REF, _FakeResolver(), retire_old=True)
    with pytest.raises(RotationAdapterError, match="resolver"):
        adapter.rotate(_FakeRef(), None, retire_old=True)


def test_get_database_failure_resets_nothing(local):
    transport, calls = _transport(get=(401, {"error": "Unauthorized"}))
    with pytest.raises(RotationAdapterError, match="HTTP 401: Unauthorized.*nothing was reset"):
        _adapter(local, transport).rotate(_FakeRef(), _FakeResolver(), retire_old=True)
    assert _resets(calls) == [] and local.access(REF) == OLD_TOKEN


def test_credential_protection_resets_nothing(local):
    """Credential protection / read-only API key: Upstash omits rest_token,
    so a reset would revoke the token without handing back the new one."""
    transport, calls = _transport(get=(200, _db(None, None, None)))
    with pytest.raises(RotationAdapterError, match="credential protection"):
        _adapter(local, transport).rotate(_FakeRef(), _FakeResolver(), retire_old=True)
    assert _resets(calls) == [] and local.access(REF) == OLD_TOKEN


def test_token_mismatch_resets_nothing(local):
    transport, calls = _transport(get=(200, _db("FAKE_some_other_db_token")))
    with pytest.raises(RotationAdapterError, match="not the current REST token") as exc:
        _adapter(local, transport).rotate(_FakeRef(), _FakeResolver(), retire_old=True)
    assert _resets(calls) == [] and local.access(REF) == OLD_TOKEN
    assert "FAKE_some_other_db_token" not in str(exc.value) and OLD_TOKEN not in str(exc.value)


# --- failures at / after the reset ----------------------------------------------

def test_reset_failure_stores_nothing_and_reports_only_error(local):
    transport, _ = _transport(reset=(403, {"error": "forbidden", "password": NEW_PW}))
    with pytest.raises(RotationAdapterError, match="HTTP 403: forbidden") as exc:
        _adapter(local, transport).rotate(_FakeRef(), _FakeResolver(), retire_old=True)
    assert local.access(REF) == OLD_TOKEN
    for secret in (API_KEY, NEW_PW, EMAIL):
        assert secret not in str(exc.value)


def test_reset_without_token_in_response_is_explicit(local):
    transport, _ = _transport(reset=(200, _db(None, None, None)))
    with pytest.raises(RotationAdapterError, match="old token is revoked.*Console"):
        _adapter(local, transport).rotate(_FakeRef(), _FakeResolver(), retire_old=True)
    assert local.access(REF) == OLD_TOKEN


def test_unchanged_token_is_refused(local):
    transport, calls = _transport(reset=(200, _db(OLD_TOKEN)))
    with pytest.raises(RotationAdapterError, match="same REST token"):
        _adapter(local, transport).rotate(_FakeRef(), _FakeResolver(), retire_old=True)
    assert not any(c[1].endswith("/ping") for c in calls)


def test_verify_failure_keeps_new_token_and_warns(local):
    audit = AuditChain()
    transport, _ = _transport(pings=[(401, {"error": "WRONGPASS invalid password"})])
    with pytest.raises(RotationAdapterError, match=r"new REST token stored.*HTTP 401") as exc:
        _adapter(local, transport, audit=audit).rotate(
            _FakeRef(), _FakeResolver(), retire_old=True)
    assert local.access(REF) == NEW_TOKEN
    assert NEW_TOKEN not in str(exc.value) and "WRONGPASS" not in str(exc.value)
    assert "warn:verify-failed" in audit.path.read_text()


def test_no_usable_rest_url_keeps_new_token(local):
    transport, _ = _transport(get=(200, _db(OLD_TOKEN, endpoint="")),
                              reset=(200, _db(NEW_TOKEN, NEW_RO, NEW_PW, endpoint="")))
    with pytest.raises(RotationAdapterError, match="no usable REST URL"):
        _adapter(local, transport).rotate(_FakeRef(), _FakeResolver(), retire_old=True)
    assert local.access(REF) == NEW_TOKEN


def test_missing_read_only_token_is_reported_after_verify(local):
    audit = AuditChain()
    transport, _ = _transport(reset=(200, _db(NEW_TOKEN, None, NEW_PW)))
    tags = {**TAGS, "upstash_read_only_token_ref": RO_REF}
    with pytest.raises(RotationAdapterError, match="stored and verified.*NOT updated"):
        _adapter(local, transport, audit=audit).rotate(
            _FakeRef(tags), _FakeResolver(), retire_old=True)
    assert local.access(REF) == NEW_TOKEN and local.access(RO_REF) == "FAKE_OLD_ro"
    assert "warn:read-only-token-not-stored" in audit.path.read_text()


def test_store_failure_after_reset_is_explicit(local):
    class Broken:
        def store(self, *a):
            raise OSError("disk full")

    transport, calls = _transport()
    with pytest.raises(RotationAdapterError, match="already revoked.*Console") as exc:
        UpstashRotationAdapter(transport=transport, local_backend=Broken(),
                               sleep=lambda s: None).rotate(
            _FakeRef(), _FakeResolver(), retire_old=True)
    assert NEW_TOKEN not in str(exc.value)
    assert not any(c[1].endswith("/ping") for c in calls)
