"""GoogleCloudAPIKeyRotationAdapter tests.

Every HTTP call goes through an injected fake transport and every gcloud call
through a fake runner -- the real API Keys API is never contacted. Keys below
are obviously fake (right shape, made-up characters).
"""
import json
import subprocess

import pytest

from portunus.audit import AuditChain
from portunus.registry import Registry
from portunus.rotation import (
    GCPServiceAccountKeyRotationAdapter,
    GoogleCloudAPIKeyRotationAdapter,
    RotationAdapterError,
    RotationResult,
    classify_google_credential,
    load_rotation_bindings,
    rotation_adapter_for,
)

API = "https://apikeys.googleapis.com/v2"
ADMIN_TOKEN = "ya29.FAKE_ADMIN_TOKEN"
OLD_KEY = "AIza" + "O" * 35
NEW_KEY = "AIza" + "N" * 35
PARENT = "projects/123456"
OLD_NAME = f"{PARENT}/locations/global/keys/old-uid-1111"
NEW_NAME = f"{PARENT}/locations/global/keys/new-uid-2222"
RESTRICTIONS = {"apiTargets": [{"service": "generativelanguage.googleapis.com"}]}
SA_JSON = json.dumps({"type": "service_account", "private_key_id": "abc", "private_key": "x"})


class _FakeResolver:
    def __init__(self, registry=None):
        self.registry = registry
        self.templates = []

    def resolve_call(self, template, boundary):
        self.templates.append(template)
        return boundary(ADMIN_TOKEN)


def _make_transport(
    *, lookup_new=NEW_NAME, create_status=200, op_done_after=0, op_error=None,
    keystring_status=200, delete_status=200, probe=(200,),
):
    calls = []
    polls = {"n": 0}
    probes = list(probe)

    def transport(method, url, headers, body, timeout):
        calls.append({"method": method, "url": url, "headers": dict(headers), "body": body})
        if url.startswith(f"{API}/keys:lookupKey"):
            if f"keyString={OLD_KEY}" in url:
                return 200, {"parent": PARENT, "name": OLD_NAME}
            if f"keyString={NEW_KEY}" in url:
                return 200, {"parent": PARENT, "name": lookup_new}
            return 404, {"error": {"status": "NOT_FOUND", "message": "no such key"}}
        if method == "GET" and url == f"{API}/{OLD_NAME}":
            return 200, {"name": OLD_NAME, "restrictions": RESTRICTIONS}
        if method == "POST" and url == f"{API}/{PARENT}/locations/global/keys":
            if create_status != 200:
                return create_status, {"error": {"status": "PERMISSION_DENIED", "message": "denied"}}
            if op_error:
                return 200, {"name": "operations/akmf.c1", "done": True, "error": op_error}
            if op_done_after == 0:
                return 200, {"name": "operations/akmf.c1", "done": True, "response": {"name": NEW_NAME}}
            return 200, {"name": "operations/akmf.c1"}
        if method == "GET" and url == f"{API}/operations/akmf.c1":
            polls["n"] += 1
            if polls["n"] >= op_done_after:
                return 200, {"name": "operations/akmf.c1", "done": True, "response": {"name": NEW_NAME}}
            return 200, {"name": "operations/akmf.c1"}
        if method == "GET" and url == f"{API}/{NEW_NAME}/keyString":
            if keystring_status != 200:
                return keystring_status, {"error": {"status": "INTERNAL", "message": "boom"}}
            return 200, {"keyString": NEW_KEY}
        if method == "DELETE":
            if url == f"{API}/{OLD_NAME}" and delete_status != 200:
                return delete_status, {"error": {"status": "PERMISSION_DENIED", "message": "nope"}}
            return 200, {"name": "operations/akmf.d1", "done": True}
        if url.startswith("https://generativelanguage.googleapis.com"):
            return (probes.pop(0) if len(probes) > 1 else probes[0]), {}
        raise AssertionError(f"unexpected call {method} {url}")

    return transport, calls


class _FakeRef:
    def __init__(self, name="flayr-prod-gemini-api-key", tags=None):
        self.name = name
        self.sm_name = name
        self.project = ""
        self.tags = {"google_api_key_admin_ref": "gcp-apikeys-admin-token"} if tags is None else tags


@pytest.fixture
def local(home):
    from portunus.localvault import LocalEncryptedBackend
    backend = LocalEncryptedBackend()
    backend.store("flayr-prod-gemini-api-key", OLD_KEY)
    return backend


def _adapter(local, transport, **kw):
    kw.setdefault("sleep", lambda s: None)
    return GoogleCloudAPIKeyRotationAdapter(transport=transport, local_backend=local, **kw)


def _calls(calls):
    out = []
    for c in calls:
        url = c["url"].replace(API, "")
        if "keyString=" in url:
            url = "/keys:lookupKey?" + ("NEW" if NEW_KEY in url else "OLD")
        out.append((c["method"], url))
    return out


# --- registry / format ----------------------------------------------------


def test_capability_is_auto():
    assert GoogleCloudAPIKeyRotationAdapter().capability() == "auto"


def test_registered_under_its_own_provider_not_gcp():
    assert isinstance(rotation_adapter_for("google-api-key"), GoogleCloudAPIKeyRotationAdapter)
    assert isinstance(rotation_adapter_for("gcp"), GCPServiceAccountKeyRotationAdapter)


@pytest.mark.parametrize("value,kind", [
    (OLD_KEY, "api-key"),
    (f"  {OLD_KEY}\n", "api-key"),
    (SA_JSON, "service-account"),
    ('{"type": "authorized_user"}', "unknown"),
    ("AIzaTooShort", "unknown"),
    ("", "unknown"),
])
def test_classify_google_credential(value, kind):
    assert classify_google_credential(value) == kind


def test_refuses_service_account_json_before_any_call(home):
    from portunus.localvault import LocalEncryptedBackend
    backend = LocalEncryptedBackend()
    backend.store("flayr-vertex-ai-prod-key", SA_JSON)
    transport, calls = _make_transport()
    with pytest.raises(RotationAdapterError, match="provider 'gcp'"):
        _adapter(backend, transport).rotate(
            _FakeRef("flayr-vertex-ai-prod-key"), resolver=_FakeResolver()
        )
    assert calls == []
    assert backend.access("flayr-vertex-ai-prod-key") == SA_JSON


def test_refuses_unrecognised_value_before_any_call(home):
    from portunus.localvault import LocalEncryptedBackend
    backend = LocalEncryptedBackend()
    backend.store("flayr-prod-gemini-api-key", "not-a-key")
    transport, calls = _make_transport()
    with pytest.raises(RotationAdapterError, match="does not look like"):
        _adapter(backend, transport).rotate(_FakeRef(), resolver=_FakeResolver())
    assert calls == []


def test_admin_ref_without_resolver_raises(local):
    transport, calls = _make_transport()
    with pytest.raises(RotationAdapterError, match="needs a resolver"):
        _adapter(local, transport).rotate(_FakeRef())
    assert calls == []


# --- happy path -------------------------------------------------------------


def test_default_finds_creates_verifies_stores_and_does_not_delete(local):
    transport, calls = _make_transport()
    result = _adapter(local, transport).rotate(_FakeRef(), resolver=_FakeResolver())
    assert isinstance(result, RotationResult)
    assert (result.provider, result.key_id, result.phase, result.retired_old) == (
        "google-api-key", "new-uid-2222", "stored", False
    )
    assert local.access("flayr-prod-gemini-api-key") == NEW_KEY
    assert _calls(calls) == [
        ("GET", "/keys:lookupKey?OLD"),
        ("GET", f"/{OLD_NAME}"),
        ("POST", f"/{PARENT}/locations/global/keys"),
        ("GET", f"/{NEW_NAME}/keyString"),
        ("GET", "/keys:lookupKey?NEW"),
    ]


def test_new_key_copies_old_restrictions_and_service_account():
    def transport_with_sa(base):
        def t(method, url, headers, body, timeout):
            if method == "GET" and url == f"{API}/{OLD_NAME}":
                return 200, {"name": OLD_NAME, "restrictions": RESTRICTIONS,
                             "serviceAccountEmail": "sa@p.iam.gserviceaccount.com"}
            return base(method, url, headers, body, timeout)
        return t

    from portunus.localvault import LocalEncryptedBackend
    local = LocalEncryptedBackend()
    local.store("flayr-prod-gemini-api-key", OLD_KEY)
    base, calls = _make_transport()
    _adapter(local, transport_with_sa(base)).rotate(_FakeRef(), resolver=_FakeResolver())
    create = next(c for c in calls if c["method"] == "POST")
    assert create["body"]["restrictions"] == RESTRICTIONS
    assert create["body"]["serviceAccountEmail"] == "sa@p.iam.gserviceaccount.com"
    assert create["body"]["displayName"].startswith("portunus-flayr-prod-gemini-api-key-")
    assert len(create["body"]["displayName"]) <= 63


def test_display_name_respects_63_char_limit(home):
    from portunus.localvault import LocalEncryptedBackend
    local = LocalEncryptedBackend()
    name = "x" * 80
    local.store(name, OLD_KEY)
    transport, calls = _make_transport()
    _adapter(local, transport).rotate(_FakeRef(name), resolver=_FakeResolver())
    create = next(c for c in calls if c["method"] == "POST")
    assert len(create["body"]["displayName"]) == 63


def test_admin_calls_use_admin_token_through_resolver(local):
    transport, calls = _make_transport()
    resolver = _FakeResolver()
    _adapter(local, transport).rotate(_FakeRef(), resolver=resolver)
    assert set(resolver.templates) == {"{{secret:gcp-apikeys-admin-token}}"}
    assert all(c["headers"]["Authorization"] == f"Bearer {ADMIN_TOKEN}" for c in calls)


def test_constructor_admin_ref_overrides_tag(local):
    transport, _ = _make_transport()
    resolver = _FakeResolver()
    _adapter(local, transport, admin_credential_ref="{{secret:other}}").rotate(
        _FakeRef(), resolver=resolver
    )
    assert set(resolver.templates) == {"{{secret:other}}"}


def test_without_admin_ref_uses_gcloud_access_token(local):
    runs = []

    def runner(cmd, **kw):
        runs.append(cmd)
        return subprocess.CompletedProcess(cmd, 0, stdout="ya29.AMBIENT\n", stderr="")

    transport, calls = _make_transport()
    _adapter(local, transport, runner=runner).rotate(_FakeRef(tags={}))
    assert runs and all(r == ["gcloud", "auth", "print-access-token"] for r in runs)
    assert all(c["headers"]["Authorization"] == "Bearer ya29.AMBIENT" for c in calls)


def test_gcloud_token_failure_raises(local):
    def runner(cmd, **kw):
        return subprocess.CompletedProcess(cmd, 1, stdout="", stderr="not logged in")

    transport, calls = _make_transport()
    with pytest.raises(RotationAdapterError, match="print-access-token failed"):
        _adapter(local, transport, runner=runner).rotate(_FakeRef(tags={}))
    assert local.access("flayr-prod-gemini-api-key") == OLD_KEY


def test_name_tag_skips_lookup_of_old_key(local):
    transport, calls = _make_transport()
    tags = {"google_api_key_admin_ref": "a", "google_api_key_name": OLD_NAME}
    _adapter(local, transport).rotate(_FakeRef(tags=tags), resolver=_FakeResolver())
    assert ("GET", "/keys:lookupKey?OLD") not in _calls(calls)


def test_create_operation_is_polled_until_done(local):
    transport, calls = _make_transport(op_done_after=3)
    sleeps = []
    _adapter(local, transport, sleep=sleeps.append).rotate(_FakeRef(), resolver=_FakeResolver())
    assert _calls(calls).count(("GET", "/operations/akmf.c1")) == 3
    assert len(sleeps) == 3
    assert local.access("flayr-prod-gemini-api-key") == NEW_KEY


def test_operation_that_never_finishes_stores_nothing(local):
    transport, _ = _make_transport(op_done_after=99)
    with pytest.raises(RotationAdapterError, match="did not finish"):
        _adapter(local, transport, poll_attempts=2).rotate(_FakeRef(), resolver=_FakeResolver())
    assert local.access("flayr-prod-gemini-api-key") == OLD_KEY


def test_operation_error_stores_nothing(local):
    transport, _ = _make_transport(op_error={"code": 7, "message": "quota"})
    with pytest.raises(RotationAdapterError, match="quota"):
        _adapter(local, transport).rotate(_FakeRef(), resolver=_FakeResolver())
    assert local.access("flayr-prod-gemini-api-key") == OLD_KEY


def test_new_key_name_written_back_to_registry_tags(home, local):
    registry = Registry()
    registry.add("flayr-prod-gemini-api-key", provider="google-api-key",
                 sm_name="flayr-prod-gemini-api-key",
                 tags={"google_api_key_admin_ref": "a"})
    ref = registry.get("flayr-prod-gemini-api-key")
    transport, _ = _make_transport()
    _adapter(local, transport).rotate(ref, resolver=_FakeResolver(registry))
    tags = Registry().get("flayr-prod-gemini-api-key").tags
    assert tags["google_api_key_name"] == NEW_NAME
    assert tags["google_api_key_admin_ref"] == "a"


def test_default_records_old_key_as_superseded(local):
    transport, _ = _make_transport()
    _adapter(local, transport).rotate(_FakeRef(), resolver=_FakeResolver())
    assert load_rotation_bindings()["google-api-key"].superseded_key_ids == (OLD_NAME,)


# --- failures ---------------------------------------------------------------


def test_create_failure_stores_nothing(local):
    transport, calls = _make_transport(create_status=403)
    with pytest.raises(RotationAdapterError, match="PERMISSION_DENIED"):
        _adapter(local, transport).rotate(_FakeRef(), resolver=_FakeResolver())
    assert local.access("flayr-prod-gemini-api-key") == OLD_KEY
    assert not any(c["method"] == "DELETE" for c in calls)


def test_old_key_lookup_failure_raises_before_create(home):
    from portunus.localvault import LocalEncryptedBackend
    backend = LocalEncryptedBackend()
    backend.store("flayr-prod-gemini-api-key", "AIza" + "Z" * 35)
    transport, calls = _make_transport()
    with pytest.raises(RotationAdapterError, match="lookup failed"):
        _adapter(backend, transport).rotate(_FakeRef(), resolver=_FakeResolver())
    assert not any(c["method"] == "POST" for c in calls)


@pytest.mark.parametrize("kw", [
    {"lookup_new": OLD_NAME},  # key string resolves to some other key
    {"keystring_status": 500},
])
def test_verify_failure_stores_nothing_and_cleans_up_new_key(local, kw):
    audit = AuditChain()
    transport, calls = _make_transport(**kw)
    with pytest.raises(RotationAdapterError, match="verification failed"):
        _adapter(local, transport, audit=audit).rotate(_FakeRef(), resolver=_FakeResolver())
    assert local.access("flayr-prod-gemini-api-key") == OLD_KEY
    assert ("DELETE", f"/{NEW_NAME}") in _calls(calls)
    assert ("DELETE", f"/{OLD_NAME}") not in _calls(calls)
    assert audit.entries()[-1]["result"] == "warn:verify-failed"


def test_verify_probe_retries_until_key_propagates(local):
    transport, calls = _make_transport(probe=(403, 403, 200))
    tags = {"google_api_key_admin_ref": "a",
            "google_api_key_verify_url": "https://generativelanguage.googleapis.com/v1beta/models"}
    _adapter(local, transport).rotate(_FakeRef(tags=tags), resolver=_FakeResolver())
    probes = [c for c in calls if "generativelanguage" in c["url"]]
    assert len(probes) == 3
    assert probes[0]["headers"]["x-goog-api-key"] == NEW_KEY
    assert "Authorization" not in probes[0]["headers"]
    assert local.access("flayr-prod-gemini-api-key") == NEW_KEY


def test_verify_probe_that_never_passes_aborts(local):
    transport, calls = _make_transport(probe=(403,))
    tags = {"google_api_key_admin_ref": "a",
            "google_api_key_verify_url": "https://generativelanguage.googleapis.com/v1beta/models"}
    with pytest.raises(RotationAdapterError, match="verification failed"):
        _adapter(local, transport, verify_attempts=3).rotate(
            _FakeRef(tags=tags), resolver=_FakeResolver()
        )
    assert local.access("flayr-prod-gemini-api-key") == OLD_KEY
    assert ("DELETE", f"/{NEW_NAME}") in _calls(calls)


def test_retire_old_deletes_old_key_after_store(local):
    transport, calls = _make_transport()
    result = _adapter(local, transport).rotate(
        _FakeRef(), resolver=_FakeResolver(), retire_old=True
    )
    assert result.retired_old is True
    assert _calls(calls)[-1] == ("DELETE", f"/{OLD_NAME}")
    assert load_rotation_bindings()["google-api-key"].superseded_key_ids == ()


def test_retire_old_delete_failure_keeps_new_key_and_superseded_record(local):
    transport, _ = _make_transport(delete_status=403)
    with pytest.raises(RotationAdapterError, match="the new key IS stored"):
        _adapter(local, transport).rotate(_FakeRef(), resolver=_FakeResolver(), retire_old=True)
    assert local.access("flayr-prod-gemini-api-key") == NEW_KEY
    assert load_rotation_bindings()["google-api-key"].superseded_key_ids == (OLD_NAME,)


def test_no_key_or_token_in_result_audit_or_errors(home, local):
    audit = AuditChain()
    transport, _ = _make_transport()
    result = _adapter(local, transport, audit=audit).rotate(_FakeRef(), resolver=_FakeResolver())
    blob = repr(result) + json.dumps(audit.entries())
    blob += (home / "rotation-bindings.json").read_text()
    for secret in (OLD_KEY, NEW_KEY, ADMIN_TOKEN):
        assert secret not in blob

    transport, _ = _make_transport(lookup_new=OLD_NAME)
    with pytest.raises(RotationAdapterError) as exc:
        _adapter(local, transport).rotate(_FakeRef(), resolver=_FakeResolver())
    for secret in (OLD_KEY, NEW_KEY, ADMIN_TOKEN):
        assert secret not in str(exc.value)
