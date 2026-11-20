"""MongoDBAtlasRotationAdapter tests (PANT-1131).

Every Atlas Admin API call goes through an injected fake transport and every
MongoDB connect through an injected connector -- neither Atlas nor a real
cluster is contacted. Credentials below are obviously fake."""
import base64
import json

import pytest

from portunus.audit import AuditChain
from portunus.mongo_atlas import (
    ATLAS_API,
    ATLAS_MEDIA_TYPE,
    ATLAS_TOKEN_URL,
    MongoDBAtlasRotationAdapter,
    MongoVerifyError,
    next_username,
    parse_uri_user,
    replace_credentials,
)
from portunus.rotation import RotationAdapterError, RotationResult, rotation_adapter_for

REF = "mercatus-liber-mongodb-uri"
USER_REF = "mercatus-liber-mongodb-username"
PW_REF = "mercatus-liber-mongodb-password"
ADMIN_REF = "atlas-admin-sa"
GROUP = "5f1e2d3c4b5a69788796a5b4"
CLIENT_ID = "mdb_sa_id_FAKE"
CLIENT_SECRET = "mdb_sa_sk_FAKE"
TOKEN = "FAKE-bearer-token"
OLD_PW = "FAKE0ldPw"
NEW_PW = "FAKEn3wPw0123456789abcdefghijklm"
HOST = "cluster0.abcde.mongodb.net"
OLD_URI = f"mongodb+srv://shopapp:{OLD_PW}@{HOST}/shop?retryWrites=true&w=majority&appName=Cluster0"
NOW = 1790000000
NEW_USER = f"shopapp-rot{NOW}"
NEW_URI = f"mongodb+srv://{NEW_USER}:{NEW_PW}@{HOST}/shop?retryWrites=true&w=majority&appName=Cluster0"
TAGS = {"atlas_group_id": GROUP, "atlas_admin_ref": ADMIN_REF}
USER_DOC = {
    "username": "shopapp", "databaseName": "admin", "groupId": GROUP,
    "roles": [{"roleName": "readWrite", "databaseName": "shop"}],
    "scopes": [{"name": "Cluster0", "type": "CLUSTER"}],
    "labels": [], "x509Type": "NONE", "awsIAMType": "NONE", "ldapAuthType": "NONE",
    "oidcAuthType": "NONE",
}
USER_PATH = f"/groups/{GROUP}/databaseUsers/admin/shopapp"
NEW_USER_PATH = f"/groups/{GROUP}/databaseUsers/admin/{NEW_USER}"


class _FakeResolver:
    def __init__(self, values=None):
        self.values = {"{{secret:%s}}" % REF: OLD_URI,
                       "{{secret:%s}}" % ADMIN_REF: f"{CLIENT_ID}:{CLIENT_SECRET}",
                       **(values or {})}

    def resolve_call(self, template, boundary):
        return boundary(self.values[template])


class _FakeRef:
    def __init__(self, tags=None, backend=""):
        self.name = REF
        self.sm_name = REF
        self.backend = backend
        self.tags = dict(TAGS) if tags is None else tags


class _Transport:
    def __init__(self, overrides=None, cluster_statuses=("PENDING", "APPLIED")):
        self.calls = []
        self.overrides = dict(overrides or {})
        self.cluster_statuses = list(cluster_statuses)

    def __call__(self, method, url, headers, body, timeout, digest):
        if url == ATLAS_TOKEN_URL:
            self.calls.append(("TOKEN", headers["Authorization"], body))
            return self.overrides.get("TOKEN", (200, {"access_token": TOKEN, "expires_in": 3600}))
        path = url.replace(ATLAS_API, "")
        parsed = json.loads(body) if body else None
        self.calls.append((method, path, headers.get("Authorization"), digest, parsed))
        assert headers["Accept"] == ATLAS_MEDIA_TYPE
        key = (method, path)
        if key in self.overrides:
            val = self.overrides[key]
            return val.pop(0) if isinstance(val, list) else val
        if method == "GET" and path in (USER_PATH,):
            return 200, dict(USER_DOC)
        if method == "POST" and path == f"/groups/{GROUP}/databaseUsers":
            return 201, {k: v for k, v in parsed.items() if k != "password"}
        if method == "PATCH" and path == USER_PATH:
            return 200, dict(USER_DOC)
        if method == "DELETE":
            return 204, {}
        if method == "GET" and path.endswith("/status"):
            s = self.cluster_statuses.pop(0) if len(self.cluster_statuses) > 1 else self.cluster_statuses[0]
            return 200, {"changeStatus": s}
        raise AssertionError(f"unexpected call {method} {path}")

    def ops(self):
        return [c[:2] for c in self.calls if c[0] != "TOKEN"]


class _Connector:
    def __init__(self, failures=0):
        self.failures = failures
        self.uris = []

    def __call__(self, uri, timeout):
        self.uris.append(uri)
        if self.failures:
            self.failures -= 1
            raise MongoVerifyError("OperationFailure code 18")


@pytest.fixture
def local(home):
    from portunus.localvault import LocalEncryptedBackend
    backend = LocalEncryptedBackend()
    backend.store(REF, OLD_URI)
    return backend


def _adapter(local, transport, connector=None, **kw):
    return MongoDBAtlasRotationAdapter(
        transport=transport, connector=connector or _Connector(), local_backend=local,
        sleep=lambda s: None, clock=lambda: NOW, password_factory=lambda: NEW_PW, **kw)


# --- registry / helpers ---------------------------------------------------------

def test_registered_and_auto():
    adapter = rotation_adapter_for("mongodb-atlas")
    assert isinstance(adapter, MongoDBAtlasRotationAdapter) and adapter.capability() == "auto"


def test_uri_helpers():
    assert parse_uri_user(OLD_URI) == ("shopapp", "")
    assert parse_uri_user("mongodb://a%40b:p@h1:27017,h2:27017/db?authSource=users") == ("a@b", "users")
    assert replace_credentials(OLD_URI, NEW_USER, NEW_PW) == NEW_URI
    assert replace_credentials("mongodb://u:p@h/db", "x@y", "p/w") == "mongodb://x%40y:p%2Fw@h/db"
    assert next_username("shopapp", 5) == "shopapp-rot5"
    assert next_username("shopapp-rot5", 9) == "shopapp-rot9"
    for bad in ("postgresql://u:p@h/db", "mongodb+srv://h/db", "mongodb+srv://u@h/db",
                "mongodb+srv://u:p@h/?authMechanism=MONGODB-X509"):
        with pytest.raises(ValueError) as exc:
            parse_uri_user(bad)
        assert "p@h" not in str(exc.value)


def test_default_password_is_long_alphanumeric():
    from portunus.mongo_atlas import generate_password
    pw = generate_password()
    assert len(pw) == 32 and pw.isalnum() and pw != generate_password()


# --- new-user mode (default) ---------------------------------------------------

def test_new_user_create_verify_store_keeps_old_user(local):
    t = _Transport()
    conn = _Connector()
    result = _adapter(local, t, conn).rotate(_FakeRef(), _FakeResolver())

    assert local.access(REF) == NEW_URI
    assert t.ops() == [("GET", USER_PATH), ("POST", f"/groups/{GROUP}/databaseUsers")]
    post = t.calls[2][4]
    assert post == {"groupId": GROUP, "databaseName": "admin", "username": NEW_USER,
                    "password": NEW_PW, "roles": USER_DOC["roles"], "scopes": USER_DOC["scopes"]}
    assert conn.uris == [NEW_URI]
    basic = base64.b64encode(f"{CLIENT_ID}:{CLIENT_SECRET}".encode()).decode()
    assert t.calls[0] == ("TOKEN", f"Basic {basic}", b"grant_type=client_credentials")
    assert sum(1 for c in t.calls if c[0] == "TOKEN") == 1  # token reused
    assert all(c[2] == f"Bearer {TOKEN}" and c[3] is None for c in t.calls[1:])
    assert isinstance(result, RotationResult)
    assert (result.provider, result.key_id, result.phase, result.retired_old) == (
        "mongodb-atlas", f"{GROUP}/{NEW_USER}", "verified", False)
    assert NEW_PW not in json.dumps(result.__dict__)


def test_new_user_retire_old_deletes_old_user(local):
    t = _Transport()
    result = _adapter(local, t).rotate(_FakeRef(), _FakeResolver(), retire_old=True)
    assert t.ops()[-1] == ("DELETE", USER_PATH)
    assert result.retired_old is True


def test_companion_refs_updated(local):
    local.store(USER_REF, "shopapp")
    local.store(PW_REF, OLD_PW)
    tags = dict(TAGS, atlas_username_ref=USER_REF, atlas_password_ref=PW_REF)
    _adapter(local, _Transport()).rotate(_FakeRef(tags), _FakeResolver())
    assert (local.access(REF), local.access(USER_REF), local.access(PW_REF)) == (
        NEW_URI, NEW_USER, NEW_PW)


def test_waits_for_cluster_applied(local):
    t = _Transport(cluster_statuses=("PENDING", "PENDING", "APPLIED"))
    tags = dict(TAGS, atlas_cluster_name="Cluster0")
    _adapter(local, t).rotate(_FakeRef(tags), _FakeResolver())
    status_path = f"/groups/{GROUP}/clusters/Cluster0/status"
    assert t.ops().count(("GET", status_path)) == 3


def test_cluster_apply_timeout_undoes_new_user(local):
    t = _Transport(cluster_statuses=("PENDING",))
    tags = dict(TAGS, atlas_cluster_name="Cluster0")
    with pytest.raises(RotationAdapterError, match="still 'PENDING'.*deleted again"):
        _adapter(local, t, apply_timeout=0).rotate(_FakeRef(tags), _FakeResolver())
    assert local.access(REF) == OLD_URI
    assert t.ops()[-1] == ("DELETE", NEW_USER_PATH)


def test_verify_retries_while_user_propagates(local):
    conn = _Connector(failures=2)
    _adapter(local, _Transport(), conn).rotate(_FakeRef(), _FakeResolver())
    assert len(conn.uris) == 3 and local.access(REF) == NEW_URI


def test_verify_failure_deletes_new_user_and_stores_nothing(local):
    audit = AuditChain()
    t = _Transport()
    with pytest.raises(RotationAdapterError) as exc:
        _adapter(local, t, _Connector(failures=99), audit=audit, verify_attempts=2).rotate(
            _FakeRef(), _FakeResolver(), retire_old=True)
    msg = str(exc.value)
    assert "OperationFailure code 18" in msg and "deleted again" in msg and "still valid" in msg
    assert local.access(REF) == OLD_URI
    assert ("DELETE", USER_PATH) not in t.ops()  # old user never retired
    assert t.ops()[-1] == ("DELETE", NEW_USER_PATH)
    assert "err:verify-failed" in audit.path.read_text()
    for secret in (NEW_PW, OLD_PW, CLIENT_SECRET, TOKEN):
        assert secret not in msg


def test_verify_failure_and_cleanup_failure_names_orphan(local):
    t = _Transport({("DELETE", NEW_USER_PATH): (500, {})})
    with pytest.raises(RotationAdapterError, match=f"remove '{NEW_USER}' in Atlas"):
        _adapter(local, t, _Connector(failures=99), verify_attempts=1).rotate(
            _FakeRef(), _FakeResolver())
    assert local.access(REF) == OLD_URI


def test_create_failure_reports_only_atlas_error(local):
    t = _Transport({("POST", f"/groups/{GROUP}/databaseUsers"):
                    (403, {"errorCode": "USER_UNAUTHORIZED", "detail": "no access", "x": NEW_PW})})
    with pytest.raises(RotationAdapterError) as exc:
        _adapter(local, t).rotate(_FakeRef(), _FakeResolver())
    assert "HTTP 403 USER_UNAUTHORIZED: no access" in str(exc.value)
    assert "nothing was changed" in str(exc.value) and NEW_PW not in str(exc.value)
    assert local.access(REF) == OLD_URI


def test_store_failure_leaves_old_user_alive(local):
    class _Failing:
        def store(self, name, value):
            raise OSError("disk full")

    t = _Transport()
    adapter = MongoDBAtlasRotationAdapter(
        transport=t, connector=_Connector(), local_backend=_Failing(),
        sleep=lambda s: None, clock=lambda: NOW, password_factory=lambda: NEW_PW)
    with pytest.raises(RotationAdapterError, match="old user was NOT retired"):
        adapter.rotate(_FakeRef(), _FakeResolver(), retire_old=True)
    assert ("DELETE", USER_PATH) not in t.ops()


def test_retire_failure_is_explicit(local):
    t = _Transport({("DELETE", USER_PATH): (404, {})})
    with pytest.raises(RotationAdapterError, match="deleting the old user 'shopapp' failed"):
        _adapter(local, t).rotate(_FakeRef(), _FakeResolver(), retire_old=True)
    assert local.access(REF) == NEW_URI


def test_audit_records_ok_without_secrets(local):
    audit = AuditChain()
    _adapter(local, _Transport(), audit=audit).rotate(_FakeRef(), _FakeResolver(), retire_old=True)
    raw = audit.path.read_text()
    assert "rotate:mongodb-atlas-user" in raw and f"ok:user={NEW_USER},retired=shopapp" in raw
    for secret in (NEW_PW, OLD_PW, CLIENT_SECRET, TOKEN, CLIENT_ID):
        assert secret not in raw


# --- password mode --------------------------------------------------------------

def test_password_mode_requires_retire_old(local):
    t = _Transport()
    with pytest.raises(RotationAdapterError, match="--retire-old"):
        _adapter(local, t).rotate(_FakeRef(dict(TAGS, atlas_rotation_mode="password")),
                                  _FakeResolver())
    assert t.calls == []


def test_password_mode_patch_store_verify(local):
    t = _Transport()
    conn = _Connector()
    tags = dict(TAGS, atlas_rotation_mode="password")
    result = _adapter(local, t, conn).rotate(_FakeRef(tags), _FakeResolver(), retire_old=True)
    expected = OLD_URI.replace(OLD_PW, NEW_PW)
    assert local.access(REF) == expected and conn.uris == [expected]
    assert t.ops() == [("GET", USER_PATH), ("PATCH", USER_PATH)]
    assert t.calls[-1][4] == {"password": NEW_PW}
    assert (result.key_id, result.retired_old) == (f"{GROUP}/shopapp", True)


def test_password_mode_verify_failure_keeps_new_password_stored(local):
    tags = dict(TAGS, atlas_rotation_mode="password")
    with pytest.raises(RotationAdapterError, match="new password stored, but connecting"):
        _adapter(local, _Transport(), _Connector(failures=99), verify_attempts=1).rotate(
            _FakeRef(tags), _FakeResolver(), retire_old=True)
    assert local.access(REF) == OLD_URI.replace(OLD_PW, NEW_PW)


# --- auth variants ---------------------------------------------------------------

def test_api_key_uses_digest_not_bearer(local):
    t = _Transport()
    tags = dict(TAGS, atlas_auth="api-key")
    resolver = _FakeResolver({"{{secret:%s}}" % ADMIN_REF: "PUBKEY:fake-private-key"})
    _adapter(local, t).rotate(_FakeRef(tags), resolver)
    assert all(c[0] != "TOKEN" for c in t.calls)
    assert all(c[2] is None and c[3] == ("PUBKEY", "fake-private-key") for c in t.calls)


def test_token_failure_changes_nothing(local):
    t = _Transport({"TOKEN": (401, {"error": "invalid_client"})})
    with pytest.raises(RotationAdapterError, match="token request failed .*HTTP 401"):
        _adapter(local, t).rotate(_FakeRef(), _FakeResolver())
    assert t.ops() == [] and local.access(REF) == OLD_URI


def test_rate_limited_call_is_retried(local):
    t = _Transport({("GET", USER_PATH): [(429, {}), (200, dict(USER_DOC))]})
    _adapter(local, t).rotate(_FakeRef(), _FakeResolver())
    assert t.ops()[:2] == [("GET", USER_PATH), ("GET", USER_PATH)]


# --- refusals before any Atlas call ---------------------------------------------

def _refuses_without_calls(local, ref, match, resolver=None, retire_old=False, **kw):
    t = _Transport()
    with pytest.raises(RotationAdapterError, match=match):
        _adapter(local, t, **kw).rotate(ref, resolver or _FakeResolver(), retire_old=retire_old)
    assert [c for c in t.calls if c[0] != "TOKEN" and c[0] != "GET"] == []
    assert local.access(REF) == OLD_URI


@pytest.mark.parametrize("missing", ["atlas_group_id", "atlas_admin_ref"])
def test_requires_tags(local, missing):
    tags = {k: v for k, v in TAGS.items() if k != missing}
    _refuses_without_calls(local, _FakeRef(tags), missing)


@pytest.mark.parametrize("tags,match", [
    (dict(TAGS, atlas_group_id="not-a-group"), "24-hex"),
    (dict(TAGS, atlas_rotation_mode="yolo"), "atlas_rotation_mode"),
    (dict(TAGS, atlas_auth="ldap"), "atlas_auth"),
])
def test_refuses_bad_tag_values(local, tags, match):
    _refuses_without_calls(local, _FakeRef(tags), match)


def test_refuses_non_local_backend(local):
    _refuses_without_calls(local, _FakeRef(backend="gcp"), "local vault")


def test_refuses_value_that_is_not_a_mongodb_uri(local):
    resolver = _FakeResolver({"{{secret:%s}}" % REF: "postgresql://u:secretpw@h/db"})
    with pytest.raises(RotationAdapterError) as exc:
        _adapter(local, _Transport()).rotate(_FakeRef(), resolver)
    assert "not usable" in str(exc.value) and "secretpw" not in str(exc.value)


def test_refuses_malformed_admin_credential(local):
    resolver = _FakeResolver({"{{secret:%s}}" % ADMIN_REF: "no-colon-here"})
    _refuses_without_calls(local, _FakeRef(), "<id>:<secret>", resolver=resolver)


def test_refuses_non_password_user(local):
    t = _Transport({("GET", USER_PATH): (200, dict(USER_DOC, x509Type="MANAGED"))})
    with pytest.raises(RotationAdapterError, match="x509Type=MANAGED"):
        _adapter(local, t).rotate(_FakeRef(), _FakeResolver())
    assert t.ops() == [("GET", USER_PATH)]


def test_read_user_404_changes_nothing(local):
    t = _Transport({("GET", USER_PATH): (404, {"errorCode": "USERNAME_NOT_FOUND"})})
    with pytest.raises(RotationAdapterError, match="USERNAME_NOT_FOUND.*nothing was changed"):
        _adapter(local, t).rotate(_FakeRef(), _FakeResolver())


def test_auth_db_from_uri_auth_source(local):
    uri = OLD_URI + "&authSource=users"
    t = _Transport({("GET", f"/groups/{GROUP}/databaseUsers/users/shopapp"): (200, dict(USER_DOC))})
    _adapter(local, t).rotate(_FakeRef(), _FakeResolver({"{{secret:%s}}" % REF: uri}))
    assert t.calls[2][4]["databaseName"] == "users"


def test_bare_name_and_missing_resolver_refused(local):
    adapter = _adapter(local, _Transport())
    with pytest.raises(RotationAdapterError, match="registry reference"):
        adapter.rotate(REF, _FakeResolver())
    with pytest.raises(RotationAdapterError, match="resolver"):
        adapter.rotate(_FakeRef(), None)


def test_missing_pymongo_refuses_before_any_call(local, monkeypatch):
    from portunus import mongo_atlas
    monkeypatch.setattr(mongo_atlas, "pymongo_available", lambda: False)
    t = _Transport()
    adapter = MongoDBAtlasRotationAdapter(transport=t, local_backend=local)
    with pytest.raises(RotationAdapterError, match=r"pantheon-portunus\[mongodb\]"):
        adapter.rotate(_FakeRef(), _FakeResolver())
    assert t.calls == []
