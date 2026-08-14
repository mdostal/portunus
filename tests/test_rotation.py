"""RotationBinding + the stub RotationAdapter registry (portunus-metadata-
and-rotation-provenance, story 02). Mirrors test_backend_router.py's shape
for VaultBinding -- this is the rotation-provenance analog, not a new
pattern. Every adapter here is a stub: `.rotate()` unconditionally raises,
matching every ARCA stub backend's own restraint (never a real API call)."""
import pytest

from portunus.rotation import (
    RotationAdapterError,
    RotationBinding,
    VercelRotationAdapter,
    GitHubRotationAdapter,
    StripeRotationAdapter,
    load_rotation_bindings,
    save_rotation_bindings,
    rotation_adapter_for,
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
