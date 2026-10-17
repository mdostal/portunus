"""NeonRotationAdapter tests (PANT-1120).

Every Neon API call goes through an injected fake transport and every
Postgres connect through an injected connector -- neither the real Neon API
nor a real database is contacted. Credentials below are obviously fake."""
import json

import pytest

from portunus import pgwire
from portunus.audit import AuditChain
from portunus.rotation import (
    NeonRotationAdapter,
    RotationAdapterError,
    RotationResult,
    rotation_adapter_for,
)

REF = "shop-mdostal-database-url-dev"
API_KEY = "napi_FAKE_ADMIN_KEY"
OLD_PW = "FAKE-old-pw"
NEW_PW = "FAKE-n3w/pw@x"
HOST = "ep-cool-name-a1b2c3-pooler.us-east-2.aws.neon.tech"
OLD_URL = f"postgresql://neondb_owner:{OLD_PW}@{HOST}/neondb?sslmode=require&channel_binding=require"
NEW_URL = OLD_URL.replace(OLD_PW, "FAKE-n3w%2Fpw%40x")
TAGS = {"neon_project_id": "proj-fake-123", "neon_branch_id": "br-fake-456",
        "neon_api_key_ref": "neon-api-key"}
API = "https://console.neon.tech/api/v2"
RESET_PATH = "/projects/proj-fake-123/branches/br-fake-456/roles/neondb_owner/reset_password"


class _FakeResolver:
    def __init__(self, values=None):
        self.templates = []
        self.values = {"{{secret:%s}}" % REF: OLD_URL, "{{secret:neon-api-key}}": API_KEY,
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


def _transport(*, reset=None, op_statuses=("running", "finished")):
    calls = []
    statuses = list(op_statuses)

    def transport(method, url, headers, body, timeout):
        calls.append((method, url.replace(API, ""), headers["Authorization"]))
        if method == "POST" and url.endswith("/reset_password"):
            if reset is not None:
                return reset.pop(0) if isinstance(reset, list) else reset
            return 200, {"role": {"name": "neondb_owner", "password": NEW_PW},
                         "operations": [{"id": "op-1", "status": "scheduling"}]}
        if method == "GET" and "/operations/" in url:
            status = statuses.pop(0) if len(statuses) > 1 else statuses[0]
            return 200, {"operation": {"id": url.rsplit("/", 1)[1], "status": status}}
        raise AssertionError(f"unexpected call {method} {url}")

    return transport, calls


class _Connector:
    def __init__(self, failures=()):
        self.failures = list(failures)
        self.calls = []

    def __call__(self, info, timeout):
        self.calls.append(info)
        if self.failures:
            raise self.failures.pop(0)


@pytest.fixture
def local(home):
    from portunus.localvault import LocalEncryptedBackend
    backend = LocalEncryptedBackend()
    backend.store(REF, OLD_URL)
    return backend


def _adapter(local, transport, connector=None, **kw):
    return NeonRotationAdapter(transport=transport, connector=connector or _Connector(),
                               local_backend=local, sleep=lambda s: None, **kw)


# --- registry / capability --------------------------------------------------

def test_registered_and_auto():
    adapter = rotation_adapter_for("neon")
    assert isinstance(adapter, NeonRotationAdapter) and adapter.capability() == "auto"


# --- happy path ---------------------------------------------------------------

def test_reset_store_wait_verify(local):
    transport, calls = _transport()
    conn = _Connector()
    result = _adapter(local, transport, conn).rotate(_FakeRef(), _FakeResolver(), retire_old=True)

    assert local.access(REF) == NEW_URL
    assert [c[:2] for c in calls] == [
        ("POST", RESET_PATH),
        ("GET", "/projects/proj-fake-123/operations/op-1"),
        ("GET", "/projects/proj-fake-123/operations/op-1"),
    ]
    assert all(c[2] == f"Bearer {API_KEY}" for c in calls)
    assert [(i.host, i.user, i.password, i.channel_binding) for i in conn.calls] == [
        (HOST, "neondb_owner", NEW_PW, "require")]
    assert isinstance(result, RotationResult)
    assert (result.provider, result.key_id, result.phase, result.retired_old) == (
        "neon", "br-fake-456/neondb_owner", "verified", True)
    assert NEW_PW not in json.dumps(result.__dict__)


def test_audit_records_ok_without_secrets(local):
    audit = AuditChain()
    transport, _ = _transport()
    _adapter(local, transport, audit=audit).rotate(_FakeRef(), _FakeResolver(), retire_old=True)
    raw = audit.path.read_text()
    assert "rotate:neon-role-password" in raw and "ok:role=neondb_owner" in raw
    for secret in (NEW_PW, OLD_PW, API_KEY):
        assert secret not in raw


def test_tagged_role_matching_connection_string_is_accepted(local):
    transport, calls = _transport()
    _adapter(local, transport).rotate(
        _FakeRef(tags={**TAGS, "neon_role_name": "neondb_owner"}), _FakeResolver(), retire_old=True)
    assert calls[0][1] == RESET_PATH


def test_verify_retries_while_password_propagates(local):
    transport, _ = _transport()
    conn = _Connector([pgwire.PgError("x", "28P01"), pgwire.PgError("x", "28P01")])
    _adapter(local, transport, conn).rotate(_FakeRef(), _FakeResolver(), retire_old=True)
    assert len(conn.calls) == 3


def test_locked_reset_is_retried(local):
    ok = (200, {"role": {"password": NEW_PW}, "operations": []})
    transport, calls = _transport(reset=[(423, {"code": "LOCKED", "message": "busy"}), ok])
    _adapter(local, transport).rotate(_FakeRef(), _FakeResolver(), retire_old=True)
    assert [c[0] for c in calls] == ["POST", "POST"] and local.access(REF) == NEW_URL


# --- refusals before anything is reset -------------------------------------------

def _refuses_without_calls(local, ref, match, resolver=None):
    transport, calls = _transport()
    with pytest.raises(RotationAdapterError, match=match):
        _adapter(local, transport).rotate(ref, resolver or _FakeResolver(), retire_old=True)
    assert calls == [] and local.access(REF) == OLD_URL


def test_requires_retire_old_ack(local):
    transport, calls = _transport()
    with pytest.raises(RotationAdapterError, match="--retire-old"):
        _adapter(local, transport).rotate(_FakeRef(), _FakeResolver())
    assert calls == []


@pytest.mark.parametrize("missing", sorted(TAGS))
def test_requires_tags(local, missing):
    tags = {k: v for k, v in TAGS.items() if k != missing}
    _refuses_without_calls(local, _FakeRef(tags=tags), missing)


def test_refuses_non_local_backend(local):
    _refuses_without_calls(local, _FakeRef(backend="gcp"), "local vault")


def test_refuses_role_tag_mismatch(local):
    _refuses_without_calls(local, _FakeRef(tags={**TAGS, "neon_role_name": "other"}),
                           "neon_role_name")


def test_refuses_value_that_is_not_a_connection_string(local):
    resolver = _FakeResolver({"{{secret:%s}}" % REF: "not-a-url-" + OLD_PW})
    transport, calls = _transport()
    with pytest.raises(RotationAdapterError) as exc:
        _adapter(local, transport).rotate(_FakeRef(), resolver, retire_old=True)
    assert calls == [] and OLD_PW not in str(exc.value)


def test_bare_name_and_missing_resolver_refused(local):
    transport, _ = _transport()
    with pytest.raises(RotationAdapterError, match="registry reference"):
        _adapter(local, transport).rotate(REF, _FakeResolver(), retire_old=True)
    with pytest.raises(RotationAdapterError, match="resolver"):
        _adapter(local, transport).rotate(_FakeRef(), None, retire_old=True)


# --- failures after the reset ------------------------------------------------------

def test_reset_failure_stores_nothing_and_reports_only_neon_error(local):
    transport, _ = _transport(reset=(403, {"code": "", "message": "permission denied"}))
    with pytest.raises(RotationAdapterError, match="HTTP 403.*permission denied") as exc:
        _adapter(local, transport).rotate(_FakeRef(), _FakeResolver(), retire_old=True)
    assert local.access(REF) == OLD_URL and API_KEY not in str(exc.value)


def test_operation_failure_keeps_new_string_stored(local):
    transport, _ = _transport(op_statuses=("failed",))
    with pytest.raises(RotationAdapterError, match="new connection string stored.*failed"):
        _adapter(local, transport).rotate(_FakeRef(), _FakeResolver(), retire_old=True)
    assert local.access(REF) == NEW_URL


def test_operation_timeout(local):
    transport, _ = _transport(op_statuses=("running",))
    with pytest.raises(RotationAdapterError, match="still 'running'"):
        _adapter(local, transport, operation_timeout=0).rotate(
            _FakeRef(), _FakeResolver(), retire_old=True)
    assert local.access(REF) == NEW_URL


def test_verify_failure_keeps_new_string_and_warns(local):
    audit = AuditChain()
    transport, _ = _transport()
    conn = _Connector([pgwire.PgError("x", "28P01")] * 5)
    with pytest.raises(RotationAdapterError, match="SQLSTATE 28P01") as exc:
        _adapter(local, transport, conn, audit=audit).rotate(
            _FakeRef(), _FakeResolver(), retire_old=True)
    assert local.access(REF) == NEW_URL
    assert NEW_PW not in str(exc.value) and HOST not in str(exc.value)
    assert "warn:verify-failed" in audit.path.read_text()


def test_store_failure_after_reset_is_explicit(local):
    class Broken:
        def store(self, *a):
            raise OSError("disk full")

    transport, _ = _transport()
    with pytest.raises(RotationAdapterError, match="reset it again") as exc:
        NeonRotationAdapter(transport=transport, connector=_Connector(), local_backend=Broken(),
                            sleep=lambda s: None).rotate(_FakeRef(), _FakeResolver(), retire_old=True)
    assert NEW_PW not in str(exc.value)
