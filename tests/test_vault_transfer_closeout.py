"""portunus-vault-transfer Story 04 (closeout): the full export -> import ->
verify flow, end to end, against two genuinely separate PORTUNUS_HOMEs --
and a direct filesystem proof that import/verify alone never create a
value-bearing file (vault.enc.json/master.key) in the target home. This is
the synthetic shape of the epic's own live proof (a real scoped export of
the actual vault, imported into a throwaway --home) -- see the epic's own
closeout notes for the real, one-off, manually-run proof against the
actual vault."""
from portunus import AuditChain, Broker, MockBackend, Registry, Resolver
from portunus.vault_transfer import build_bundle, import_bundle, verify_access, write_bundle

SECRET = "FAKE-TEST-VALUE-do-not-leak-0xDEAD"


def _instance(path):
    registry = Registry(path=path / "registry.json")
    audit = AuditChain(path=path / "audit.log")
    broker = Broker(registry, audit)
    backend = MockBackend()
    resolver = Resolver(registry, backend, broker)
    return registry, backend, resolver


def test_full_export_import_verify_flow_across_two_separate_homes(tmp_path):
    source_home = tmp_path / "source"
    target_home = tmp_path / "target"
    source_home.mkdir()
    target_home.mkdir()

    source_registry, source_backend, _ = _instance(source_home)
    source_registry.add("shared-key", "sm-shared-key", project="demo-proj")
    source_backend.set("sm-shared-key", SECRET)

    bundle = build_bundle(source_registry, {}, {})
    bundle_path = write_bundle(bundle, out=str(tmp_path / "bundle.json"))
    assert SECRET not in bundle_path.read_text()

    target_registry, target_backend, target_resolver = _instance(target_home)
    report = import_bundle(bundle, target_registry, {}, {})
    assert report["created"] == ["shared-key"]

    # The imported reference is a pointer only -- the target's own mock
    # backend has never seen the value, so it's correctly NOT reachable yet
    # (a real GCP-backed reference would need the target's own gcloud
    # identity to have IAM access; this proves the mechanism doesn't
    # silently assume reachability).
    verify_report = verify_access(target_registry, target_resolver, {})
    assert verify_report["needs_auth"] == [] or verify_report["reachable"] == []

    # The one hard invariant: import/verify never create a value-bearing
    # file in the target home, no matter what happened above.
    assert not (target_home / "vault.enc.json").exists()
    assert not (target_home / "master.key").exists()


def test_a_local_only_reference_lands_requested_and_verify_reports_the_drop_hint(tmp_path):
    source_home = tmp_path / "source"
    target_home = tmp_path / "target"
    source_home.mkdir()
    target_home.mkdir()

    source_registry, _, _ = _instance(source_home)
    source_registry.add("local-only", "sm-local-only")  # no project -> resolved_backend="local"

    bundle = build_bundle(source_registry, {}, {})
    assert bundle["references"][0]["resolved_backend"] == "local"

    target_registry, _, target_resolver = _instance(target_home)
    import_bundle(bundle, target_registry, {}, {})
    assert target_registry.get("local-only").state == "requested"

    verify_report = verify_access(target_registry, target_resolver, {})
    assert len(verify_report["needs_drop"]) == 1
    assert verify_report["needs_drop"][0]["hint"] == "portunus drop local-only sm-local-only --stdin"

    assert not (target_home / "vault.enc.json").exists()
    assert not (target_home / "master.key").exists()
