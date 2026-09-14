"""RotationBinding + the stub RotationAdapter registry (portunus-metadata-
and-rotation-provenance, story 02). Mirrors test_backend_router.py's shape
for VaultBinding -- this is the rotation-provenance analog, not a new
pattern. Every adapter here is a stub: `.rotate()` unconditionally raises,
matching every ARCA stub backend's own restraint (never a real API call)."""
import dataclasses
import pytest

from portunus.audit import AuditChain
from portunus.rotation import (
    OAuthRefreshRotationAdapter,
    RotationAdapterError,
    RotationBinding,
    RotationResult,
    VercelRotationAdapter,
    GitHubRotationAdapter,
    StripeRotationAdapter,
    audit_rotate,
    load_rotation_bindings,
    rotation_adapter_for,
    run_periodic_oauth_refresh,
    save_rotation_bindings,
)


def test_rotation_binding_defaults_to_stub_status():
    binding = RotationBinding(provider="vercel")
    assert binding.status == "stub"
    assert binding.account == ""


@pytest.mark.parametrize("adapter_cls,provider_name", [
    (VercelRotationAdapter, "Vercel"),
    (GitHubRotationAdapter, "GitHub"),
    (StripeRotationAdapter, "Stripe"),
])
def test_stub_adapters_unconditionally_raise(adapter_cls, provider_name):
    adapter = adapter_cls()
    with pytest.raises(RotationAdapterError) as exc_info:
        adapter.rotate(ref=None)
    message = str(exc_info.value)
    assert provider_name in message
    assert "github.com" in message
    assert "adapter-request" in message


def test_rotation_bindings_round_trip(home):
    bindings = load_rotation_bindings()
    assert bindings == {}

    save_rotation_bindings({
        "vercel": RotationBinding(provider="vercel", status="stub", account="my-team-slug"),
    })

    reloaded = load_rotation_bindings()
    assert reloaded["vercel"].provider == "vercel"
    assert reloaded["vercel"].status == "stub"
    assert reloaded["vercel"].account == "my-team-slug"


def test_rotation_bindings_file_is_0600(home):
    save_rotation_bindings({"vercel": RotationBinding(provider="vercel", account="x")})
    path = home / "rotation-bindings.json"
    assert path.exists()
    assert oct(path.stat().st_mode)[-3:] == "600"


def test_rotation_adapter_for_resolves_by_provider():
    assert isinstance(rotation_adapter_for("vercel"), VercelRotationAdapter)
    assert isinstance(rotation_adapter_for("github"), GitHubRotationAdapter)
    assert isinstance(rotation_adapter_for("stripe"), StripeRotationAdapter)


def test_rotation_adapter_for_unknown_provider_returns_none():
    assert rotation_adapter_for("some-unheard-of-provider") is None


# --- OAuthRefreshRotationAdapter ------------------------------------------


def test_rotation_adapter_for_oauth_returns_real_adapter():
    adapter = rotation_adapter_for("oauth")
    assert isinstance(adapter, OAuthRefreshRotationAdapter)


def test_rotation_result_has_no_credential_fields():
    """RotationResult must be structurally incapable of holding credential material."""
    field_names = {f.name for f in dataclasses.fields(RotationResult)}
    # Only identifiers, phase, booleans, timestamps -- never a secret value.
    assert "token" not in field_names
    assert "access_token" not in field_names
    assert "refresh_token" not in field_names
    assert "secret" not in field_names
    assert "credential" not in field_names
    assert "client_secret" not in field_names
    assert "provider" in field_names
    assert "ref_name" in field_names
    assert "phase" in field_names
    assert "retired_old" in field_names
    assert "timestamp" in field_names


def test_oauth_adapter_retire_old_is_rejected():
    adapter = OAuthRefreshRotationAdapter()
    with pytest.raises(RotationAdapterError) as exc_info:
        adapter.rotate("oauth-provider:account", retire_old=True)
    msg = str(exc_info.value)
    assert "retire" in msg.lower()
    assert "not applicable" in msg.lower()


def _make_transport(access_token="NEW.ACCESS.TOKEN", rotated_refresh=None):
    """Returns an injected transport + call log. If rotated_refresh is set,
    the response includes a new refresh_token (provider-rotated)."""
    calls = []

    def transport(url, data, headers, timeout):
        calls.append({"url": url, "data": dict(data)})
        resp = {"access_token": access_token, "expires_in": 3600}
        if rotated_refresh:
            resp["refresh_token"] = rotated_refresh
        return resp

    return transport, calls


CREDENTIAL = {
    "client_id": "client-123",
    "client_secret": "secret",
    "refresh_token": "REFRESH-TOKEN",
    "token_endpoint": "https://oauth2.example.com/token",
}


def _seed(home, provider="myprovider", account="user@example.com", credential=None):
    from portunus.localvault import LocalEncryptedBackend
    local = LocalEncryptedBackend()
    local.store_oauth_credential(provider, account, credential or CREDENTIAL)
    return local


def test_oauth_adapter_rotate_delegates_to_oauth_backend(home):
    """rotate() delegates to OAuthBackend -- no second refresh grant."""
    local = _seed(home)
    transport, calls = _make_transport()
    adapter = OAuthRefreshRotationAdapter(local_backend=local, transport=transport)

    result = adapter.rotate("myprovider:user@example.com")

    # Transport was called exactly once -- the OAuthBackend path
    assert len(calls) == 1
    assert calls[0]["data"]["refresh_token"] == "REFRESH-TOKEN"


def test_oauth_adapter_rotate_returns_rotation_result(home):
    local = _seed(home)
    transport, _ = _make_transport()
    adapter = OAuthRefreshRotationAdapter(local_backend=local, transport=transport)

    result = adapter.rotate("myprovider:user@example.com")

    assert isinstance(result, RotationResult)
    assert result.provider == "myprovider"
    assert result.phase == "refreshed"
    assert result.retired_old is False


def test_oauth_adapter_rotate_result_contains_no_token(home):
    """RotationResult must not contain the access token or any credential value."""
    local = _seed(home)
    transport, _ = _make_transport(access_token="VERY.SECRET.ACCESS.TOKEN")
    adapter = OAuthRefreshRotationAdapter(local_backend=local, transport=transport)

    result = adapter.rotate("myprovider:user@example.com")

    result_str = str(result)
    assert "VERY.SECRET.ACCESS.TOKEN" not in result_str
    assert "REFRESH-TOKEN" not in result_str


def test_oauth_adapter_persists_rotated_refresh_token_exactly_once(home):
    """When provider returns a new refresh_token, it is persisted once via
    store_oauth_credential -- no second copy, no duplication."""
    from portunus.localvault import LocalEncryptedBackend
    local = LocalEncryptedBackend()
    local.store_oauth_credential("myprovider", "user@example.com", CREDENTIAL)

    transport, _ = _make_transport(rotated_refresh="NEW-REFRESH-TOKEN")
    adapter = OAuthRefreshRotationAdapter(local_backend=local, transport=transport)
    adapter.rotate("myprovider:user@example.com")

    # The stored credential should now have the NEW refresh token
    record = local.load_oauth_credential("myprovider", "user@example.com")
    assert record["credential"]["refresh_token"] == "NEW-REFRESH-TOKEN"


def test_oauth_adapter_rotate_ref_object_with_sm_name(home):
    """rotate() accepts a Reference-like object as well as a plain string."""
    local = _seed(home)
    transport, calls = _make_transport()
    adapter = OAuthRefreshRotationAdapter(local_backend=local, transport=transport)

    class FakeRef:
        sm_name = "myprovider:user@example.com"
        name = "my-ref-name"

    result = adapter.rotate(FakeRef())
    assert result.ref_name == "my-ref-name"
    assert len(calls) == 1


def test_periodic_refresh_drives_all_stored_credentials(home):
    """run_periodic_oauth_refresh() iterates list_oauth_credentials() and
    drives every entry through the single adapter -- no per-provider job."""
    from portunus.localvault import LocalEncryptedBackend
    local = LocalEncryptedBackend()
    local.store_oauth_credential("provA", "a@example.com", CREDENTIAL)
    local.store_oauth_credential("provB", "b@example.com", CREDENTIAL)

    transport, calls = _make_transport()
    results = run_periodic_oauth_refresh(
        local_backend=local, transport=transport
    )

    assert len(results) == 2
    assert len(calls) == 2
    phases = {r.phase for r in results}
    assert phases == {"refreshed"}


def test_periodic_refresh_empty_vault_returns_empty_list(home):
    results = run_periodic_oauth_refresh()
    assert results == []


# --- PANT-158: "manual" status, RotationAdapter protocol, audit_rotate -------


def test_rotation_binding_manual_status_round_trips(home):
    """'manual' survives save/load unchanged."""
    save_rotation_bindings({
        "linear": RotationBinding(provider="linear", status="manual", account=""),
    })
    reloaded = load_rotation_bindings()
    assert reloaded["linear"].status == "manual"


def test_rotation_result_field_set_includes_key_id():
    """key_id is present -- an identifier, never credential material."""
    field_names = {f.name for f in dataclasses.fields(RotationResult)}
    assert "key_id" in field_names


@pytest.mark.parametrize("adapter_cls", [
    VercelRotationAdapter,
    GitHubRotationAdapter,
    StripeRotationAdapter,
])
def test_stub_adapters_capability_returns_unknown(adapter_cls):
    assert adapter_cls().capability() == "unknown"


def test_oauth_adapter_capability_returns_auto():
    assert OAuthRefreshRotationAdapter().capability() == "auto"


@pytest.mark.parametrize("adapter_cls", [
    VercelRotationAdapter,
    GitHubRotationAdapter,
    StripeRotationAdapter,
])
def test_stub_adapters_rotate_accepts_retire_old_kwarg(adapter_cls):
    """retire_old=False must be accepted without TypeError -- existing tests
    still verify that rotate() raises RotationAdapterError."""
    adapter = adapter_cls()
    with pytest.raises(RotationAdapterError):
        adapter.rotate(ref=None, retire_old=False)


def test_audit_rotate_ok_appends_rotate_action(home):
    audit = AuditChain()
    audit_rotate(audit, "my-ref", "ok:created")
    entries = audit.entries()
    assert len(entries) == 1
    assert entries[0]["action"] == "rotate"
    assert entries[0]["secret"] == "my-ref"
    assert entries[0]["result"] == "ok:created"


def test_audit_rotate_error_appends_err_result(home):
    audit = AuditChain()
    audit_rotate(audit, "my-ref", "err:backend-timeout")
    entries = audit.entries()
    assert entries[0]["result"].startswith("err:")


def test_audit_verify_intact_after_rotate_entries(home):
    audit = AuditChain()
    audit_rotate(audit, "ref-a", "ok:created")
    audit_rotate(audit, "ref-a", "warn:verify-failed")
    audit_rotate(audit, "ref-a", "ok:retired")
    assert audit.verify() is True
