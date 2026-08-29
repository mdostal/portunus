"""portunus-vault-transfer Story 01: `portunus vault access export` -- a
scoped, plain-JSON, metadata-only bundle (never passphrase-locked, because
it structurally cannot contain a secret value). Each reference also carries
a precomputed `resolved_backend` field, computed the same way the real
backend router (`cli.py::_make_backend_router`) would resolve it right now
on THIS instance -- not a separately-reasoned-about duplicate."""
import json

import pytest

from portunus import Registry
from portunus.backend import VaultBinding, save_vault_bindings
from portunus.rotation import RotationBinding, save_rotation_bindings
from portunus.vault_transfer import build_bundle, resolved_backend_kind, write_bundle


def _ref(**kwargs):
    from portunus.registry import Reference
    defaults = dict(name="x", sm_name="sm-x", state="enabled")
    defaults.update(kwargs)
    return Reference(**defaults)


# --- resolved_backend_kind() -- mirrors the real router, string-only -------

def test_resolved_backend_explicit_override_wins():
    ref = _ref(backend="aws", project="p")
    assert resolved_backend_kind(ref, {}) == "aws"


def test_resolved_backend_falls_back_to_project_binding():
    ref = _ref(project="p")
    bindings = {"p": VaultBinding(project="p", backend="gcp")}
    assert resolved_backend_kind(ref, bindings) == "gcp"


def test_resolved_backend_cached_gcp_binding_still_reports_gcp():
    """SyncingBackend wraps GCP for cached-mode -- still fundamentally
    GCP-backed for the "is this local" question this field exists to
    answer, not a distinct backend kind."""
    ref = _ref(project="p")
    bindings = {"p": VaultBinding(project="p", backend="gcp", sync_mode="cached")}
    assert resolved_backend_kind(ref, bindings) == "gcp"


def test_resolved_backend_falls_back_to_global_default_when_unset(monkeypatch):
    monkeypatch.delenv("PORTUNUS_BACKEND", raising=False)
    ref = _ref(project="totally-unconfigured")
    assert resolved_backend_kind(ref, {}) == "local"


def test_resolved_backend_normalizes_the_gcloud_env_var_spelling(monkeypatch):
    """A real, pre-existing inconsistency: PORTUNUS_BACKEND=gcloud selects
    the GCP backend, but every OTHER kind string in this codebase
    (ref.backend, VaultBinding.backend) spells it "gcp". This field must
    report the SAME vocabulary those other fields use, not the env var's
    own spelling."""
    monkeypatch.setenv("PORTUNUS_BACKEND", "gcloud")
    ref = _ref(project="totally-unconfigured")
    assert resolved_backend_kind(ref, {}) == "gcp"


def test_resolved_backend_matches_the_real_router_for_real(monkeypatch, tmp_path):
    """Not just reasoned-about-separately -- directly compared against
    cli.py's own _make_backend_router() for a real set of scenarios."""
    from portunus.audit import AuditChain
    from portunus.cli import _make_backend_router
    from portunus.localvault import LocalEncryptedBackend

    monkeypatch.setenv("PORTUNUS_HOME", str(tmp_path))
    monkeypatch.delenv("PORTUNUS_BACKEND", raising=False)
    bindings = {"p-gcp": VaultBinding(project="p-gcp", backend="gcp")}
    router = _make_backend_router(bindings, AuditChain(), LocalEncryptedBackend())

    scenarios = [
        _ref(name="a", project="p-gcp"),           # via binding
        _ref(name="b", project="unconfigured"),    # via global fallback (local)
        _ref(name="c", project="p-gcp", backend="aws"),  # explicit override wins
    ]
    kind_by_class = {"LocalEncryptedBackend": "local", "GcloudBackend": "gcp", "AWSSecretsManagerBackend": "aws"}
    for ref in scenarios:
        real_kind = kind_by_class[type(router(ref)).__name__]
        assert resolved_backend_kind(ref, bindings) == real_kind, ref.name


# --- build_bundle() ----------------------------------------------------------

def test_build_bundle_no_filter_includes_everything(home):
    reg = Registry()
    reg.add("a", "sm-a", project="p1")
    reg.add("b", "sm-b", project="p2")
    bundle = build_bundle(reg, {}, {})
    names = {e["name"] for e in bundle["references"]}
    assert names == {"a", "b"}


def test_build_bundle_project_filter(home):
    reg = Registry()
    reg.add("a", "sm-a", project="p1")
    reg.add("b", "sm-b", project="p2")
    bundle = build_bundle(reg, {}, {}, project="p1")
    names = {e["name"] for e in bundle["references"]}
    assert names == {"a"}


def test_build_bundle_org_filter(home):
    reg = Registry()
    reg.add("a", "sm-a", org="firefly-events")
    reg.add("b", "sm-b", org="other-org")
    bundle = build_bundle(reg, {}, {}, org="firefly-events")
    names = {e["name"] for e in bundle["references"]}
    assert names == {"a"}


def test_build_bundle_tags_filter_supports_repo(home):
    reg = Registry()
    reg.add("a", "sm-a", repo="event-api")
    reg.add("b", "sm-b", repo="other-repo")
    bundle = build_bundle(reg, {}, {}, tags="repo=event-api")
    names = {e["name"] for e in bundle["references"]}
    assert names == {"a"}


def test_build_bundle_combined_filters_are_and_semantics(home):
    reg = Registry()
    reg.add("a", "sm-a", project="p1", env="prod")
    reg.add("b", "sm-b", project="p1", env="dev")
    reg.add("c", "sm-c", project="p2", env="prod")
    bundle = build_bundle(reg, {}, {}, project="p1", tags="env=prod")
    names = {e["name"] for e in bundle["references"]}
    assert names == {"a"}


def test_build_bundle_includes_project_binding(home):
    reg = Registry()
    reg.add("a", "sm-a", project="p1")
    bindings = {"p1": VaultBinding(project="p1", backend="gcp", account="user@example.com")}
    bundle = build_bundle(reg, bindings, {})
    assert bundle["vault_bindings"]["p1"]["account"] == "user@example.com"


def test_build_bundle_binding_included_even_without_direct_project_match(home):
    """A binding is optional metadata -- absence never excludes the reference."""
    reg = Registry()
    reg.add("a", "sm-a", project="p1")
    bundle = build_bundle(reg, {}, {})
    assert bundle["vault_bindings"] == {}
    names = {e["name"] for e in bundle["references"]}
    assert names == {"a"}


def test_build_bundle_includes_rotation_binding_by_provider(home):
    """RotationBinding is keyed by provider, not project -- a real
    correction from the epic's own planning docs, verified against
    rotation.py's actual load_rotation_bindings() shape directly."""
    reg = Registry()
    reg.add("a", "sm-a", provider="vercel")
    rotation_bindings = {"vercel": RotationBinding(provider="vercel", status="stub", account="team-slug")}
    bundle = build_bundle(reg, {}, rotation_bindings)
    assert bundle["rotation_bindings"]["vercel"]["account"] == "team-slug"


def test_build_bundle_dedups_shared_bindings(home):
    reg = Registry()
    reg.add("a", "sm-a", project="p1")
    reg.add("b", "sm-b", project="p1")
    bindings = {"p1": VaultBinding(project="p1", backend="gcp")}
    bundle = build_bundle(reg, bindings, {})
    assert len(bundle["vault_bindings"]) == 1


def test_build_bundle_each_reference_carries_resolved_backend(home, monkeypatch):
    monkeypatch.delenv("PORTUNUS_BACKEND", raising=False)
    reg = Registry()
    reg.add("a", "sm-a", backend="aws")
    bundle = build_bundle(reg, {}, {})
    assert bundle["references"][0]["resolved_backend"] == "aws"


def test_write_bundle_default_output_path_never_collides_with_vault_export(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    path = write_bundle({"references": [], "vault_bindings": {}, "rotation_bindings": {}}, out=None)
    assert path.name != "portunus-vault-export.pvault"
    assert json.loads(path.read_text()) == {"references": [], "vault_bindings": {}, "rotation_bindings": {}}


def test_write_bundle_respects_out_path(tmp_path):
    target = tmp_path / "custom-bundle.json"
    path = write_bundle({"references": [], "vault_bindings": {}, "rotation_bindings": {}}, out=str(target))
    assert path == target
    assert path.exists()


# --- structural secret-boundary guard ---------------------------------------

def test_vault_transfer_export_path_never_imports_secret_backend_machinery():
    """This module has zero legitimate reason to ever see a value -- it
    only ever writes registry/bindings metadata."""
    import ast
    import inspect
    import portunus.vault_transfer as vt

    tree = ast.parse(inspect.getsource(vt))
    imported_names = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.ImportFrom):
            imported_names.update(alias.name for alias in node.names)
        elif isinstance(node, ast.Import):
            imported_names.update(alias.name for alias in node.names)
    forbidden = {"LocalEncryptedBackend", "GcloudBackend", "SecretBackend", "Broker"}
    assert not (imported_names & forbidden)
