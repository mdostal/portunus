"""GrafanaCloudRotationAdapter tests.

Every HTTP call goes through an injected fake transport -- the real Grafana
Cloud API is never contacted. The fake records each call (method, url, the
bearer token used, body) so assertions can check exactly what was sent, and
with which credential.
"""
import pytest

from portunus.rotation import (
    GrafanaCloudRotationAdapter,
    RotationAdapterError,
    RotationResult,
    load_rotation_bindings,
    rotation_adapter_for,
)

API = "https://www.grafana.com/api/v1"
ADMIN_TOKEN = "glc_FAKE-ADMIN"
OLD_TOKEN = "glc_FAKE-OLD"
NEW_TOKEN = "glc_FAKE-NEW"
POLICY = "c45485b6-8321-4cf2-bcec-12006df755ff"


class _FakeResolver:
    """Stands in for Resolver.resolve_call(): hands the admin token only to
    the boundary and records which template was asked for."""

    def __init__(self, registry=None):
        self.templates = []
        self.registry = registry

    def resolve_call(self, template, boundary):
        self.templates.append(template)
        return boundary(ADMIN_TOKEN)


class _FakeRegistry:
    def __init__(self, ref):
        self.refs = {ref.name: ref}
        self.retags = []

    def __contains__(self, name):
        return name in self.refs

    def retag(self, name, *, tags=None):
        self.retags.append((name, tags))
        self.refs[name].tags = tags


class _FakeRef:
    def __init__(self, name="monitoring-dev-grafana-cloud-api-token", **tags):
        self.name = name
        self.sm_name = name
        self.tags = {
            "grafana_access_policy_id": POLICY,
            "grafana_region": "prod-us-east-0",
            **tags,
        }
        self.tags = {k: v for k, v in self.tags.items() if v is not None}


def _transport(*, create=(200, None), verify=200, delete=204):
    calls = []

    def transport(method, url, headers, body, timeout):
        token = headers["Authorization"].removeprefix("Bearer ")
        calls.append({"method": method, "url": url, "token": token, "body": body})
        if method == "POST" and url.startswith(f"{API}/tokens"):
            status, resp = create
            if resp is None:
                resp = {"id": "tok-new", "accessPolicyId": POLICY, "token": NEW_TOKEN}
            return status, resp
        if method == "GET":
            return verify, {}
        if method == "DELETE":
            return delete, {}
        raise AssertionError(f"unexpected call {method} {url}")

    return transport, calls


@pytest.fixture
def local(home):
    from portunus.localvault import LocalEncryptedBackend
    backend = LocalEncryptedBackend()
    backend.store("monitoring-dev-grafana-cloud-api-token", OLD_TOKEN)
    return backend


def test_capability_and_registration():
    assert GrafanaCloudRotationAdapter().capability() == "auto"
    assert isinstance(rotation_adapter_for("grafana-cloud"), GrafanaCloudRotationAdapter)
    # Self-hosted admin passwords / stack data-source creds are NOT this adapter.
    assert rotation_adapter_for("grafana") is None


def test_rotate_creates_verifies_stores_without_retiring(local):
    transport, calls = _transport()
    ref = _FakeRef(grafana_token_id="tok-old")
    registry = _FakeRegistry(ref)
    adapter = GrafanaCloudRotationAdapter(transport=transport, local_backend=local)

    result = adapter.rotate(ref, resolver=_FakeResolver(registry))

    assert isinstance(result, RotationResult)
    assert (result.provider, result.key_id, result.phase, result.retired_old) == (
        "grafana-cloud", "tok-new", "stored", False)
    assert NEW_TOKEN not in repr(result)

    create, verify = calls
    assert create["method"] == "POST"
    assert create["url"] == f"{API}/tokens?region=prod-us-east-0"
    assert create["token"] == ADMIN_TOKEN
    assert create["body"]["accessPolicyId"] == POLICY
    assert create["body"]["name"].startswith("portunus-monitoring-dev-grafana-cloud-api-token-")
    assert "expiresAt" not in create["body"]
    # Verify is a read-only GET made with the NEW token.
    assert verify == {"method": "GET", "url": f"{API}/accesspolicies/{POLICY}?region=prod-us-east-0",
                      "token": NEW_TOKEN, "body": None}

    assert local.access("monitoring-dev-grafana-cloud-api-token") == NEW_TOKEN
    assert registry.refs[ref.name].tags["grafana_token_id"] == "tok-new"
    assert load_rotation_bindings()["grafana-cloud"].superseded_key_ids == ("tok-old",)


def test_retire_old_deletes_old_token_and_clears_superseded(local):
    transport, calls = _transport()
    adapter = GrafanaCloudRotationAdapter(transport=transport, local_backend=local)

    result = adapter.rotate(_FakeRef(grafana_token_id="tok-old"),
                            resolver=_FakeResolver(), retire_old=True)

    assert result.retired_old is True
    delete = calls[-1]
    assert delete["method"] == "DELETE"
    assert delete["url"] == f"{API}/tokens/tok-old?region=prod-us-east-0"
    assert delete["token"] == ADMIN_TOKEN
    assert load_rotation_bindings()["grafana-cloud"].superseded_key_ids == ()


def test_retire_old_404_counts_as_retired(local):
    transport, _ = _transport(delete=404)
    adapter = GrafanaCloudRotationAdapter(transport=transport, local_backend=local)
    result = adapter.rotate(_FakeRef(grafana_token_id="tok-old"),
                            resolver=_FakeResolver(), retire_old=True)
    assert result.retired_old is True


def test_retire_failure_keeps_new_token_and_superseded_entry(local):
    transport, _ = _transport(delete=500)
    adapter = GrafanaCloudRotationAdapter(transport=transport, local_backend=local)

    with pytest.raises(RotationAdapterError, match="new token IS stored"):
        adapter.rotate(_FakeRef(grafana_token_id="tok-old"),
                       resolver=_FakeResolver(), retire_old=True)

    assert local.access("monitoring-dev-grafana-cloud-api-token") == NEW_TOKEN
    assert load_rotation_bindings()["grafana-cloud"].superseded_key_ids == ("tok-old",)


def test_retire_old_without_token_id_refuses_before_creating(local):
    transport, calls = _transport()
    adapter = GrafanaCloudRotationAdapter(transport=transport, local_backend=local)
    with pytest.raises(RotationAdapterError, match="grafana_token_id"):
        adapter.rotate(_FakeRef(), resolver=_FakeResolver(), retire_old=True)
    assert calls == []


def test_verify_failure_stores_nothing_and_cleans_up_new_token(local):
    transport, calls = _transport(verify=401)
    adapter = GrafanaCloudRotationAdapter(transport=transport, local_backend=local)

    with pytest.raises(RotationAdapterError, match="verification failed") as exc:
        adapter.rotate(_FakeRef(grafana_token_id="tok-old"),
                       resolver=_FakeResolver(), retire_old=True)

    assert NEW_TOKEN not in str(exc.value)
    assert local.access("monitoring-dev-grafana-cloud-api-token") == OLD_TOKEN
    deletes = [c for c in calls if c["method"] == "DELETE"]
    # Only the never-stored NEW token is deleted -- the old one is untouched.
    assert [d["url"] for d in deletes] == [f"{API}/tokens/tok-new?region=prod-us-east-0"]
    assert "grafana-cloud" not in load_rotation_bindings()


def test_verify_403_is_a_failure(local):
    transport, _ = _transport(verify=403)
    adapter = GrafanaCloudRotationAdapter(transport=transport, local_backend=local)
    with pytest.raises(RotationAdapterError, match="verification failed"):
        adapter.rotate(_FakeRef(), resolver=_FakeResolver())


def test_create_failure_raises_without_leaking_body(local):
    transport, calls = _transport(
        create=(403, {"code": "Forbidden", "message": "missing accesspolicies:write"}))
    adapter = GrafanaCloudRotationAdapter(transport=transport, local_backend=local)
    with pytest.raises(RotationAdapterError, match="HTTP 403 Forbidden"):
        adapter.rotate(_FakeRef(), resolver=_FakeResolver())
    assert len(calls) == 1
    assert local.access("monitoring-dev-grafana-cloud-api-token") == OLD_TOKEN


def test_admin_ref_tag_selects_admin_credential(local):
    transport, _ = _transport()
    resolver = _FakeResolver()
    adapter = GrafanaCloudRotationAdapter(transport=transport, local_backend=local)
    adapter.rotate(_FakeRef(grafana_admin_ref="grafana-cloud-admin"), resolver=resolver)
    assert set(resolver.templates) == {"{{secret:grafana-cloud-admin}}"}


def test_admin_ref_defaults_to_the_ref_itself(local):
    transport, _ = _transport()
    resolver = _FakeResolver()
    adapter = GrafanaCloudRotationAdapter(transport=transport, local_backend=local)
    adapter.rotate(_FakeRef(), resolver=resolver)
    assert set(resolver.templates) == {"{{secret:monitoring-dev-grafana-cloud-api-token}}"}


def test_ttl_days_sets_expires_at(local):
    transport, calls = _transport()
    adapter = GrafanaCloudRotationAdapter(transport=transport, local_backend=local)
    adapter.rotate(_FakeRef(grafana_token_ttl_days="90"), resolver=_FakeResolver())
    assert calls[0]["body"]["expiresAt"].endswith("Z")


@pytest.mark.parametrize("ttl", ["0", "-3", "soon"])
def test_bad_ttl_rejected(local, ttl):
    transport, calls = _transport()
    adapter = GrafanaCloudRotationAdapter(transport=transport, local_backend=local)
    with pytest.raises(RotationAdapterError, match="grafana_token_ttl_days"):
        adapter.rotate(_FakeRef(grafana_token_ttl_days=ttl), resolver=_FakeResolver())
    assert calls == []


def test_verify_path_override(local):
    transport, calls = _transport()
    adapter = GrafanaCloudRotationAdapter(transport=transport, local_backend=local)
    adapter.rotate(_FakeRef(grafana_verify_path="/api/orgs/acme"), resolver=_FakeResolver())
    assert calls[1]["url"] == "https://grafana.com/api/orgs/acme"
    assert calls[1]["token"] == NEW_TOKEN


def test_verify_path_must_be_an_api_path(local):
    transport, calls = _transport()
    adapter = GrafanaCloudRotationAdapter(transport=transport, local_backend=local)
    with pytest.raises(RotationAdapterError, match="grafana_verify_path"):
        adapter.rotate(_FakeRef(grafana_verify_path="@evil.example/x"), resolver=_FakeResolver())
    assert calls == []


@pytest.mark.parametrize("missing", ["grafana_access_policy_id", "grafana_region"])
def test_missing_required_tags(local, missing):
    transport, calls = _transport()
    adapter = GrafanaCloudRotationAdapter(transport=transport, local_backend=local)
    with pytest.raises(RotationAdapterError, match=missing):
        adapter.rotate(_FakeRef(**{missing: None}), resolver=_FakeResolver())
    assert calls == []


def test_requires_resolver(local):
    adapter = GrafanaCloudRotationAdapter(transport=_transport()[0], local_backend=local)
    with pytest.raises(RotationAdapterError, match="needs a resolver"):
        adapter.rotate(_FakeRef())


def test_token_name_is_slugged():
    name = GrafanaCloudRotationAdapter._token_name("My_Ref.Name!!")
    assert name.startswith("portunus-my-ref-name-")
    assert len(GrafanaCloudRotationAdapter._token_name("x" * 500)) <= 100


def test_audit_trail(local):
    class _Audit:
        def __init__(self):
            self.entries = []

        def append(self, action, ref, result):
            self.entries.append((action, ref, result))

    audit = _Audit()
    transport, _ = _transport()
    adapter = GrafanaCloudRotationAdapter(transport=transport, local_backend=local, audit=audit)
    adapter.rotate(_FakeRef(), resolver=_FakeResolver())
    assert audit.entries == [
        ("rotate:grafana-cloud-token", "monitoring-dev-grafana-cloud-api-token", "ok:new=tok-new")]
