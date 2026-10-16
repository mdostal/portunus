"""YouTrackRotationAdapter tests.

Every Hub REST call goes through an injected fake transport -- no real Hub
is ever contacted. The fake models one Hub user with a set of permanent
tokens, so assertions can check exactly which tokens exist afterwards.
"""
from types import SimpleNamespace

import pytest

from portunus.rotation import (
    RotationAdapterError,
    YouTrackRotationAdapter,
    load_rotation_bindings,
    rotation_adapter_for,
)

HUB = "https://yt.example.com/hub"
API = HUB + "/api/rest"
USER_ID = "user-1"
OLD_TOKEN = "perm:FAKE-OLD"
OLD_TOKEN_ID = "tok-old"
SCOPE = [{"id": "svc-youtrack"}, {"id": "svc-hub"}]


class _FakeHub:
    def __init__(self, *, create_ok=True, new_token_works=True, delete_ok=True):
        # token value -> {"id", "scope"}
        self.tokens = {OLD_TOKEN: {"id": OLD_TOKEN_ID, "scope": list(SCOPE)}}
        self.create_ok = create_ok
        self.new_token_works = new_token_works
        self.delete_ok = delete_ok
        self.calls = []
        self.created_bodies = []

    def _by_id(self, token_id):
        return next((v for v in self.tokens.values() if v["id"] == token_id), None)

    def __call__(self, method, url, headers, body, timeout):
        self.calls.append((method, url))
        bearer = headers["Authorization"].split(" ", 1)[1]
        if bearer not in self.tokens:
            return 401, None
        if not self.new_token_works and bearer != OLD_TOKEN:
            return 401, None
        path = url[len(API):].split("?", 1)[0]
        tokens_prefix = f"/users/{USER_ID}/permanenttokens"
        if method == "GET" and path == "/users/me":
            return 200, {"id": USER_ID}
        if method == "POST" and path == tokens_prefix:
            if not self.create_ok:
                return 403, {"error": "forbidden"}
            self.created_bodies.append(body)
            new_id = f"tok-new-{len(self.created_bodies)}"
            value = f"perm:FAKE-NEW-{len(self.created_bodies)}"
            self.tokens[value] = {"id": new_id, "scope": body["scope"]}
            return 200, {"id": new_id, "token": value}
        if path.startswith(tokens_prefix + "/"):
            token_id = path.rsplit("/", 1)[1]
            entry = self._by_id(token_id)
            if entry is None:
                return 404, None
            if method == "GET":
                return 200, {"id": token_id, "scope": entry["scope"]}
            if method == "DELETE":
                if not self.delete_ok:
                    return 500, None
                self.tokens = {k: v for k, v in self.tokens.items() if v["id"] != token_id}
                return 200, None
        return 404, None


class _FakeBackend:
    def __init__(self):
        self.values = {"yt-token": OLD_TOKEN}

    def access(self, sm_name, project=""):
        return self.values[sm_name]

    def store(self, sm_name, value):
        self.values[sm_name] = value


class _FakeAudit:
    def __init__(self):
        self.entries = []

    def append(self, action, ref, result):
        self.entries.append((action, ref, result))


def _ref(**tags):
    base = {"hub_url": HUB, "hub_token_id": OLD_TOKEN_ID}
    base.update(tags)
    return SimpleNamespace(name="yt-api-key", sm_name="yt-token", tags={k: v for k, v in base.items() if v is not None})


class _FakeRegistry:
    def __init__(self, names=("yt-api-key",)):
        self.names = set(names)
        self.retagged = {}

    def __contains__(self, name):
        return name in self.names

    def retag(self, name, *, tags):
        self.retagged[name] = tags


@pytest.fixture(autouse=True)
def _isolated_home(home):
    # superseded token ids land in PORTUNUS_HOME/rotation-bindings.json
    return home


def _adapter(hub=None, registry=None):
    hub = hub or _FakeHub()
    backend, audit = _FakeBackend(), _FakeAudit()
    adapter = YouTrackRotationAdapter(
        transport=hub, local_backend=backend, audit=audit, registry=registry
    )
    return adapter, hub, backend, audit


def test_registered_and_auto():
    adapter = rotation_adapter_for("youtrack")
    assert isinstance(adapter, YouTrackRotationAdapter)
    assert adapter.capability() == "auto"


def test_rotate_creates_verifies_and_stores_without_retiring():
    registry = _FakeRegistry()
    adapter, hub, backend, audit = _adapter(registry=registry)
    result = adapter.rotate(_ref())

    assert backend.values["yt-token"] == "perm:FAKE-NEW-1"
    assert result.provider == "youtrack"
    assert result.key_id == "tok-new-1"
    assert result.phase == "stored"
    assert result.retired_old is False
    # old token untouched; new token carries the old token's scope
    assert OLD_TOKEN in hub.tokens
    assert hub.created_bodies[0]["scope"] == SCOPE
    assert not any(m == "DELETE" for m, _ in hub.calls)
    assert audit.entries[-1] == ("rotate:youtrack", "yt-token", "ok:new=tok-new-1")
    # new id recorded on the reference, old id listed as superseded
    assert registry.retagged["yt-api-key"] == {"hub_url": HUB, "hub_token_id": "tok-new-1"}
    assert load_rotation_bindings()["youtrack"].superseded_key_ids == (OLD_TOKEN_ID,)
    # no token value in the result or the audit trail
    assert "perm:" not in repr(result) + repr(audit.entries)


def test_retire_old_deletes_old_token():
    adapter, hub, backend, _ = _adapter()
    result = adapter.rotate(_ref(), retire_old=True)
    assert result.retired_old is True
    assert OLD_TOKEN not in hub.tokens
    assert set(hub.tokens) == {"perm:FAKE-NEW-1"}
    assert ("DELETE", f"{API}/users/{USER_ID}/permanenttokens/{OLD_TOKEN_ID}") in hub.calls
    assert load_rotation_bindings()["youtrack"].superseded_key_ids == ()


def test_registry_falls_back_to_resolver_registry():
    registry = _FakeRegistry()
    adapter, _, _, _ = _adapter()
    adapter.rotate(_ref(), resolver=SimpleNamespace(registry=registry))
    assert registry.retagged["yt-api-key"]["hub_token_id"] == "tok-new-1"


def test_retire_old_without_token_id_fails_before_any_call():
    adapter, hub, backend, _ = _adapter()
    with pytest.raises(RotationAdapterError, match="hub_token_id"):
        adapter.rotate(_ref(hub_token_id=None, hub_scope="svc-youtrack"), retire_old=True)
    assert hub.calls == []
    assert backend.values["yt-token"] == OLD_TOKEN


def test_hub_scope_tag_used_when_no_token_id():
    adapter, hub, _, _ = _adapter()
    adapter.rotate(_ref(hub_token_id=None, hub_scope="svc-a, svc-b"))
    assert hub.created_bodies[0]["scope"] == [{"id": "svc-a"}, {"id": "svc-b"}]


def test_no_scope_source_raises_and_creates_nothing():
    adapter, hub, backend, _ = _adapter()
    with pytest.raises(RotationAdapterError, match="hub_scope"):
        adapter.rotate(_ref(hub_token_id=None))
    assert hub.created_bodies == []
    assert backend.values["yt-token"] == OLD_TOKEN


def test_verify_failure_stores_nothing_and_removes_new_token():
    adapter, hub, backend, audit = _adapter(_FakeHub(new_token_works=False))
    with pytest.raises(RotationAdapterError, match="verification failed"):
        adapter.rotate(_ref(), retire_old=True)
    assert backend.values["yt-token"] == OLD_TOKEN
    assert set(hub.tokens) == {OLD_TOKEN}
    assert ("rotate:youtrack", "yt-token", "warn:verify-failed") in audit.entries


def test_create_failure_leaves_everything_intact():
    adapter, hub, backend, _ = _adapter(_FakeHub(create_ok=False))
    with pytest.raises(RotationAdapterError, match="HTTP 403"):
        adapter.rotate(_ref())
    assert backend.values["yt-token"] == OLD_TOKEN
    assert set(hub.tokens) == {OLD_TOKEN}


def test_rejected_stored_token_raises():
    adapter, hub, backend, _ = _adapter()
    backend.values["yt-token"] = "perm:REVOKED"
    with pytest.raises(RotationAdapterError, match="rejected"):
        adapter.rotate(_ref())


def test_retire_failure_reports_new_token_is_stored():
    adapter, hub, backend, _ = _adapter(_FakeHub(delete_ok=False))
    with pytest.raises(RotationAdapterError, match="IS stored"):
        adapter.rotate(_ref(), retire_old=True)
    assert backend.values["yt-token"] == "perm:FAKE-NEW-1"
    assert load_rotation_bindings()["youtrack"].superseded_key_ids == (OLD_TOKEN_ID,)


def test_missing_hub_url_raises():
    adapter, hub, _, _ = _adapter()
    with pytest.raises(RotationAdapterError, match="no Hub URL"):
        adapter.rotate(_ref(hub_url=None))
    assert hub.calls == []


@pytest.mark.parametrize(
    "youtrack_url, hub_url",
    [
        ("https://yt.example.com", "https://yt.example.com/hub"),
        ("https://yt.example.com/", "https://yt.example.com/hub"),
        ("https://acme.myjetbrains.com/youtrack", "https://acme.myjetbrains.com/hub"),
    ],
)
def test_hub_url_derived_from_youtrack_url(youtrack_url, hub_url):
    adapter = YouTrackRotationAdapter()
    assert adapter._hub_base_url("r", {"youtrack_url": youtrack_url}, None) == hub_url


def test_hub_url_from_stored_reference_via_resolver():
    seen = []

    class _Resolver:
        def resolve_call(self, template, boundary):
            seen.append(template)
            return boundary("https://yt.example.com\n")

    adapter, hub, _, _ = _adapter()
    adapter.rotate(
        _ref(hub_url=None, youtrack_url_ref="ffe-orchestration-dev-youtrack-base-url"),
        resolver=_Resolver(),
    )
    assert seen == ["{{secret:ffe-orchestration-dev-youtrack-base-url}}"]
    assert hub.calls[0][1].startswith(API + "/")
