"""RotationBinding + the stub RotationAdapter registry (portunus-metadata-
and-rotation-provenance, story 02) and rotation audit (story 03). Mirrors
test_backend_router.py's shape for VaultBinding -- this is the rotation-
provenance analog, not a new pattern. Every adapter here is a stub:
`.rotate()` unconditionally raises, matching every ARCA stub backend's own
restraint (never a real API call)."""
import json

import pytest

from portunus.rotation import (
    RotationAdapterError,
    RotationBinding,
    VercelRotationAdapter,
    GitHubRotationAdapter,
    StripeRotationAdapter,
    capability_for_status,
    load_rotation_bindings,
    rotation_audit_data,
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


# --- RotationBinding.status = "manual" -------------------------------------

def test_rotation_binding_manual_status_survives_round_trip(home):
    save_rotation_bindings({
        "linear": RotationBinding(provider="linear", status="manual"),
    })
    reloaded = load_rotation_bindings()
    assert reloaded["linear"].status == "manual"


def test_rotation_binding_manual_renders_distinctly(home, capsys):
    from portunus.cli import main
    main(["rotation-bindings", "set", "linear", "--status", "manual"])
    capsys.readouterr()
    rc = main(["rotation-bindings", "show", "linear"])
    out = capsys.readouterr().out
    assert rc == 0
    assert "manual" in out


# --- capability_for_status -------------------------------------------------

@pytest.mark.parametrize("status,expected", [
    ("real",   "auto"),
    ("stub",   "unknown"),
    ("manual", "manual"),
    ("",       "unknown"),
])
def test_capability_for_status(status, expected):
    assert capability_for_status(status) == expected


# --- superseded_key_ids ----------------------------------------------------

def test_superseded_key_ids_default_empty():
    b = RotationBinding(provider="gcp")
    assert b.superseded_key_ids == ()


def test_superseded_key_ids_round_trip(home):
    save_rotation_bindings({
        "gcp": RotationBinding(
            provider="gcp", status="real",
            superseded_key_ids=("old-key-1", "old-key-2"),
        ),
    })
    reloaded = load_rotation_bindings()
    assert reloaded["gcp"].superseded_key_ids == ("old-key-1", "old-key-2")


# --- rotation_audit_data ---------------------------------------------------

class _FakeRegistry:
    """Minimal registry stub for audit tests."""
    def __init__(self, refs):
        self._refs = refs

    def __iter__(self):
        return iter(self._refs)


class _FakeRef:
    def __init__(self, name, provider=""):
        self.name = name
        self.provider = provider


class _FakeLocalBackend:
    """Minimal local backend stub returning canned OAuth credentials."""
    def __init__(self, credentials=None, raise_on_call=False):
        self._creds = credentials or []
        self._raise = raise_on_call

    def list_oauth_credentials(self):
        if self._raise:
            raise RuntimeError("simulated vault error")
        return self._creds


def _cred(provider, account):
    return {"namespace": {"provider": provider, "account": account}}


def test_audit_groups_refs_by_provider(home):
    registry = _FakeRegistry([
        _FakeRef("gcp-sa-key", "gcp"),
        _FakeRef("gcp-api-key", "gcp"),
        _FakeRef("stripe-key", "stripe"),
    ])
    bindings = {}
    report = rotation_audit_data(registry, bindings)
    assert sorted(report["providers"]["gcp"]["refs"]) == ["gcp-api-key", "gcp-sa-key"]
    assert report["providers"]["stripe"]["refs"] == ["stripe-key"]


def test_audit_no_bindings_reports_all_unknown(home):
    registry = _FakeRegistry([_FakeRef("some-ref", "github")])
    report = rotation_audit_data(registry, {})
    assert report["providers"]["github"]["capability"] == "unknown"


def test_audit_manual_capability_rendered_distinctly(home):
    registry = _FakeRegistry([_FakeRef("lin-key", "linear")])
    bindings = {"linear": RotationBinding(provider="linear", status="manual")}
    report = rotation_audit_data(registry, bindings)
    assert report["providers"]["linear"]["capability"] == "manual"


def test_audit_auto_capability_for_real_binding(home):
    registry = _FakeRegistry([_FakeRef("gcp-key", "gcp")])
    bindings = {"gcp": RotationBinding(provider="gcp", status="real")}
    report = rotation_audit_data(registry, bindings)
    assert report["providers"]["gcp"]["capability"] == "auto"


def test_audit_superseded_keys_reported_per_provider(home):
    registry = _FakeRegistry([_FakeRef("gcp-key", "gcp")])
    bindings = {
        "gcp": RotationBinding(
            provider="gcp", status="real",
            superseded_key_ids=("old-key-abc123",),
        ),
    }
    report = rotation_audit_data(registry, bindings)
    assert "old-key-abc123" in report["providers"]["gcp"]["superseded_key_ids"]


def test_audit_corrupt_oauth_credential_counted_not_fatal(home):
    registry = _FakeRegistry([])
    bindings = {}
    bad_backend = _FakeLocalBackend(raise_on_call=True)
    report = rotation_audit_data(registry, bindings, bad_backend)
    assert report["unreadable_oauth_count"] == 1


def test_audit_includes_oauth_credentials(home):
    registry = _FakeRegistry([])
    bindings = {}
    backend = _FakeLocalBackend([_cred("anthropic", "user@example.com")])
    report = rotation_audit_data(registry, bindings, backend)
    assert "user@example.com" in report["providers"]["anthropic"]["oauth_accounts"]


def test_audit_empty_vault_exits_zero(home, capsys):
    from portunus.cli import main
    rc = main(["rotation", "audit"])
    assert rc == 0


def test_audit_json_flag_emits_machine_readable(home, capsys):
    from portunus.cli import main
    rc = main(["rotation", "audit", "--json"])
    out = capsys.readouterr().out
    assert rc == 0
    data = json.loads(out)
    assert "providers" in data
    assert "totals" in data
    assert "unreadable_oauth_count" in data


def test_audit_json_contains_no_credential_material(home, capsys):
    """The JSON output must never include fields that could carry a value."""
    from portunus.cli import main
    rc = main(["rotation", "audit", "--json"])
    out = capsys.readouterr().out
    assert rc == 0
    data = json.loads(out)
    for prov_data in data["providers"].values():
        assert "value" not in prov_data
        assert "secret" not in prov_data
        assert "token" not in prov_data
