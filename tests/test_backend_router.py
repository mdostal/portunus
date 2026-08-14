"""The actual per-project/per-reference backend router (story 02,
portunus-vault-routing) -- closes the gap between backend.py's own
docstring claim ("selected per-Reference by provider+project") and the
prior reality (one global PORTUNUS_BACKEND per process)."""
import json
from types import SimpleNamespace

import pytest

from portunus import Registry
from portunus.backend import VaultBinding, load_vault_bindings, save_vault_bindings
from portunus.cli import _build


def _mock_gcloud(monkeypatch, value="FROM-GCLOUD"):
    observed = []

    def fake_run(cmd, capture_output, text, timeout):
        observed.append(cmd)
        return SimpleNamespace(returncode=0, stdout=value, stderr="")

    monkeypatch.setattr("portunus.backend.subprocess.run", fake_run)
    monkeypatch.setattr("portunus.backend.shutil.which", lambda name: "/bin/gcloud")
    return observed


def test_ref_backend_override_wins_over_project_binding(home, monkeypatch):
    """A reference explicitly set to backend='local' resolves locally even
    though its project is bound to backend='gcp' -- precedence level 1."""
    _mock_gcloud(monkeypatch)
    save_vault_bindings({"demo": VaultBinding("demo", backend="gcp")})
    registry = Registry()
    registry.add("x", "sm-x", project="demo", backend="local")

    from portunus.localvault import LocalEncryptedBackend
    LocalEncryptedBackend().store("sm-x", "LOCAL-VALUE")

    _, _, _, resolver = _build()
    seen = {}
    resolver.resolve_call("{{secret:x}}", lambda v: seen.setdefault("v", v))
    assert seen["v"] == "LOCAL-VALUE"


def test_project_binding_wins_when_no_ref_override(home, monkeypatch):
    """No ref.backend set -- routes via the project's VaultBinding (gcp),
    precedence level 2."""
    observed = _mock_gcloud(monkeypatch, value="FROM-GCLOUD")
    save_vault_bindings({"demo": VaultBinding("demo", backend="gcp")})
    registry = Registry()
    registry.add("x", "sm-x", project="demo")

    _, _, _, resolver = _build()
    seen = {}
    resolver.resolve_call("{{secret:x}}", lambda v: seen.setdefault("v", v))
    assert seen["v"] == "FROM-GCLOUD"
    assert "--project=demo" in observed[0]


def test_falls_back_to_global_backend_when_no_binding(home, monkeypatch):
    """No ref.backend, no VaultBinding for the project at all -- falls back
    to today's global PORTUNUS_BACKEND, unchanged (precedence level 3)."""
    monkeypatch.setenv("PORTUNUS_BACKEND", "mock")
    monkeypatch.setenv("PORTUNUS_MOCK_SM_X", "FROM-GLOBAL-MOCK")
    registry = Registry()
    registry.add("x", "sm-x", project="unbound-project")

    _, _, _, resolver = _build()
    seen = {}
    resolver.resolve_call("{{secret:x}}", lambda v: seen.setdefault("v", v))
    assert seen["v"] == "FROM-GLOBAL-MOCK"


def test_mock_backend_env_short_circuits_router_entirely(home, monkeypatch):
    """PORTUNUS_BACKEND=mock must never let a vault-bindings.json entry
    reach for a real GcloudBackend (grill H2) -- a safety rail for this
    session's whole test suite, not an edge case."""
    monkeypatch.setenv("PORTUNUS_BACKEND", "mock")
    monkeypatch.setenv("PORTUNUS_MOCK_SM_X", "FROM-MOCK")
    # A real, matching binding is present -- must still be ignored under mock.
    save_vault_bindings({"demo": VaultBinding("demo", backend="gcp")})
    registry = Registry()
    registry.add("x", "sm-x", project="demo")

    _, _, _, resolver = _build()
    seen = {}
    resolver.resolve_call("{{secret:x}}", lambda v: seen.setdefault("v", v))
    assert seen["v"] == "FROM-MOCK"


def test_two_projects_route_to_different_backends_in_same_process(home, monkeypatch):
    """The actual headline fix: per-reference routing within ONE process,
    not one backend per process as before this epic."""
    observed = _mock_gcloud(monkeypatch, value="FROM-GCLOUD")
    save_vault_bindings({
        "local-proj": VaultBinding("local-proj", backend="local"),
        "gcp-proj": VaultBinding("gcp-proj", backend="gcp"),
    })
    registry = Registry()
    registry.add("a", "sm-a", project="local-proj")
    registry.add("b", "sm-b", project="gcp-proj")

    from portunus.localvault import LocalEncryptedBackend
    LocalEncryptedBackend().store("sm-a", "LOCAL-VALUE")

    _, _, _, resolver = _build()
    seen = {}
    resolver.resolve_call("{{secret:a}}", lambda v: seen.setdefault("a", v))
    resolver.resolve_call("{{secret:b}}", lambda v: seen.setdefault("b", v))
    assert seen["a"] == "LOCAL-VALUE"
    assert seen["b"] == "FROM-GCLOUD"
    assert "--project=gcp-proj" in observed[0]


def test_router_wraps_cached_gcp_binding_in_syncing_backend(home, monkeypatch):
    """A project bound with sync_mode='cached' routes through SyncingBackend
    -- second access serves from the local cache, no redundant gcloud
    value-fetch (story 03's wiring into the router)."""
    import json as _json
    from types import SimpleNamespace as _NS

    calls = []

    def fake_run(cmd, capture_output, text, timeout):
        calls.append(cmd)
        if "describe" in cmd:
            return _NS(returncode=0, stdout=_json.dumps({"name": "v1", "createTime": "T1"}), stderr="")
        return _NS(returncode=0, stdout="CACHED-VALUE", stderr="")

    monkeypatch.setattr("portunus.backend.subprocess.run", fake_run)
    monkeypatch.setattr("portunus.backend.shutil.which", lambda name: "/bin/gcloud")
    save_vault_bindings({"demo": VaultBinding("demo", backend="gcp", sync_mode="cached")})
    registry = Registry()
    registry.add("x", "sm-x", project="demo")

    _, _, _, resolver = _build()
    seen = {}
    resolver.resolve_call("{{secret:x}}", lambda v: seen.setdefault("first", v))
    resolver.resolve_call("{{secret:x}}", lambda v: seen.setdefault("second", v))
    assert seen["first"] == "CACHED-VALUE"
    assert seen["second"] == "CACHED-VALUE"
    # 3 calls: describe+access on first, describe-only on second (cache hit)
    assert len(calls) == 3


def test_backend_gate_no_longer_requires_manual_portunus_backend_env(home, monkeypatch):
    """The exact friction point from this session: PORTUNUS_BACKEND=gcloud
    should no longer be REQUIRED for a bound project to resolve correctly."""
    _mock_gcloud(monkeypatch, value="FROM-GCLOUD")
    save_vault_bindings({"demo": VaultBinding("demo", backend="gcp")})
    registry = Registry()
    registry.add("x", "sm-x", project="demo")
    # deliberately NOT setting PORTUNUS_BACKEND -- default is "local"
    assert "PORTUNUS_BACKEND" not in __import__("os").environ

    _, _, _, resolver = _build()
    seen = {}
    resolver.resolve_call("{{secret:x}}", lambda v: seen.setdefault("v", v))
    assert seen["v"] == "FROM-GCLOUD"


# --- story 01 (portunus-swappable-trio): 5 new stub kinds route cleanly --

@pytest.mark.parametrize("kind,name_fragment", [
    ("vault", "vault"),
    ("infisical", "infisical"),
    ("doppler", "doppler"),
    ("onepassword", "1password"),
    ("azure", "azure"),
])
def test_router_routes_stub_kinds_and_fails_closed_cleanly(home, kind, name_fragment):
    from portunus.backend import BackendError
    save_vault_bindings({"demo": VaultBinding("demo", backend=kind)})
    registry = Registry()
    registry.add("x", "sm-x", project="demo")

    _, _, _, resolver = _build()
    with pytest.raises(BackendError) as exc_info:
        resolver.resolve_call("{{secret:x}}", lambda v: None)
    assert name_fragment in str(exc_info.value).lower()


def test_router_caches_the_same_stub_instance_per_kind(home):
    save_vault_bindings({
        "a": VaultBinding("a", backend="vault"),
        "b": VaultBinding("b", backend="vault"),
    })
    registry = Registry()
    registry.add("x", "sm-x", project="a")
    registry.add("y", "sm-y", project="b")

    _, _, _, resolver = _build()
    backend_x = resolver.backend_for(registry.require("x"))
    backend_y = resolver.backend_for(registry.require("y"))
    assert backend_x is backend_y


def test_bindings_set_show_round_trips_new_stub_kinds(home):
    from portunus.cli import main
    import json
    rc = main(["bindings", "set", "demo", "--backend", "infisical"])
    assert rc == 0
    bindings = load_vault_bindings()
    assert bindings["demo"].backend == "infisical"
