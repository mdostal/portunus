"""portunus-vault-transfer Story 02: `portunus vault access import` --
reconstructs registry + bindings on the target from a Story 01 bundle.
The one piece of real logic: resolved_backend=="local" always forces
state="requested" on import, regardless of the source's own state -- the
value literally doesn't exist anywhere the target can reach. Every other
backend's state transfers unchanged. A per-reference conflict never aborts
the rest of the batch (matches drop_bulk's own established precedent)."""
from portunus import Registry
from portunus.backend import VaultBinding
from portunus.rotation import RotationBinding
from portunus.vault_transfer import build_bundle, import_bundle


def _bundle(references, vault_bindings=None, rotation_bindings=None):
    return {
        "format_version": 1,
        "references": references,
        "vault_bindings": vault_bindings or {},
        "rotation_bindings": rotation_bindings or {},
    }


def _entry(**kwargs):
    defaults = dict(
        name="x", sm_name="sm-x", scope="", kind="", state="enabled", approval="",
        sm_path="", org="", provider="", project="", env="", tags={}, description="",
        purpose="", injected_as={}, group="", related=[], backend="", repo="",
        source_files=[], suggested={}, resolved_backend="gcp",
    )
    defaults.update(kwargs)
    return defaults


# --- the resolved_backend=local forcing rule --------------------------------

def test_import_forces_requested_state_for_local_backend_regardless_of_source_state(home):
    reg = Registry()
    bundle = _bundle([_entry(name="a", state="enabled", resolved_backend="local")])
    import_bundle(bundle, reg, {}, {})
    assert reg.get("a").state == "requested"


def test_import_forces_requested_even_when_source_state_was_already_requested(home):
    reg = Registry()
    bundle = _bundle([_entry(name="a", state="requested", resolved_backend="local")])
    import_bundle(bundle, reg, {}, {})
    assert reg.get("a").state == "requested"


def test_import_passes_through_state_unchanged_for_non_local_backends(home):
    reg = Registry()
    bundle = _bundle([_entry(name="a", state="enabled", resolved_backend="gcp")])
    import_bundle(bundle, reg, {}, {})
    assert reg.get("a").state == "enabled"


def test_import_pins_the_targets_backend_field_to_the_precomputed_resolved_backend(home):
    """Story 02 never re-derives resolved_backend -- it just writes the
    Story 01-computed value straight into the target's ref.backend field,
    pinning it explicitly regardless of the target's own env/bindings."""
    reg = Registry()
    bundle = _bundle([_entry(name="a", resolved_backend="aws")])
    import_bundle(bundle, reg, {}, {})
    assert reg.get("a").backend == "aws"


# --- bindings upsert ----------------------------------------------------------

def test_import_upserts_bundled_vault_binding_without_clobbering_unrelated_ones(home):
    reg = Registry()
    target_bindings = {"other-proj": VaultBinding(project="other-proj", backend="aws")}
    bundle = _bundle([], vault_bindings={"p1": {"project": "p1", "backend": "gcp", "wif_audience": "", "account": "", "sync_mode": "direct"}})
    import_bundle(bundle, reg, target_bindings, {})
    assert target_bindings["p1"].backend == "gcp"
    assert target_bindings["other-proj"].backend == "aws"


def test_import_upserts_bundled_rotation_binding(home):
    reg = Registry()
    target_rotation = {}
    bundle = _bundle([], rotation_bindings={"vercel": {"provider": "vercel", "status": "stub", "account": "team-slug"}})
    import_bundle(bundle, reg, {}, target_rotation)
    assert target_rotation["vercel"].account == "team-slug"


# --- clean create / safe no-op re-import --------------------------------------

def test_import_creates_cleanly_when_name_does_not_exist(home):
    reg = Registry()
    bundle = _bundle([_entry(name="brand-new")])
    report = import_bundle(bundle, reg, {}, {})
    assert "brand-new" in reg
    assert report["created"] == ["brand-new"]


def test_import_is_a_safe_no_op_on_identical_re_import(home):
    reg = Registry()
    bundle = _bundle([_entry(name="a", sm_name="sm-a", resolved_backend="gcp", state="enabled")])
    import_bundle(bundle, reg, {}, {})
    report = import_bundle(bundle, reg, {}, {})
    assert report == {"created": [], "updated": [], "conflicted": [], "skipped": ["a"]}
    assert reg.get("a").sm_name == "sm-a"


# --- conflict handling ---------------------------------------------------------

def test_import_refuses_a_conflicting_entry_without_force_but_processes_the_rest(home):
    reg = Registry()
    reg.add("a", "sm-a-old", backend="gcp")
    bundle = _bundle([
        _entry(name="a", sm_name="sm-a-new", resolved_backend="aws"),
        _entry(name="b", sm_name="sm-b"),
    ])
    report = import_bundle(bundle, reg, {}, {}, force=False)
    assert reg.get("a").sm_name == "sm-a-old"  # untouched
    assert reg.get("b") is not None  # non-conflicting entry still processed
    assert len(report["conflicted"]) == 1
    conflict = report["conflicted"][0]
    assert conflict["name"] == "a"
    assert conflict["existing_sm_name"] == "sm-a-old"
    assert conflict["new_sm_name"] == "sm-a-new"
    assert report["created"] == ["b"]


def test_import_overwrites_a_conflicting_entry_with_force(home):
    reg = Registry()
    reg.add("a", "sm-a-old", backend="gcp")
    bundle = _bundle([_entry(name="a", sm_name="sm-a-new", resolved_backend="aws")])
    report = import_bundle(bundle, reg, {}, {}, force=True)
    assert reg.get("a").sm_name == "sm-a-new"
    assert reg.get("a").backend == "aws"
    assert report["conflicted"] == []
    assert report["updated"] == ["a"]


# --- report shape --------------------------------------------------------------

def test_import_report_always_has_all_four_count_buckets(home):
    reg = Registry()
    report = import_bundle(_bundle([]), reg, {}, {})
    assert set(report.keys()) == {"created", "updated", "conflicted", "skipped"}


# --- round-trip with the real build_bundle() ------------------------------------

def test_import_round_trips_a_real_export_bundle(home, monkeypatch, tmp_path):
    monkeypatch.setenv("PORTUNUS_BACKEND", "mock")
    source_reg = Registry()
    source_reg.add("a", "sm-a", project="p1")
    bundle = build_bundle(source_reg, {}, {})
    target_reg = Registry(path=tmp_path / "target-registry.json")
    report = import_bundle(bundle, target_reg, {}, {})
    assert report["created"] == ["a"]
    assert target_reg.get("a").sm_name == "sm-a"


# --- structural secret-boundary guard -------------------------------------------

def test_vault_transfer_import_path_never_imports_secret_backend_machinery():
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
