"""`portunus vault export`/`portunus vault import` (portunus-vault-backup
story 03). CLI-only for v1 (no MCP tool, no UI surface -- an archive
containing every secret in the vault should never be triggerable by an
LLM-facing tool without a human directly initiating it)."""
import json

import pytest

from portunus import Registry
from portunus.backend import VaultBinding, save_vault_bindings
from portunus.backup import ExportError, export_archive, import_archive
from portunus.cli import main
from portunus.localvault import LocalEncryptedBackend


def _populate_vault():
    Registry().add("x", "sm-x", project="demo", description="a secret")
    LocalEncryptedBackend().store("sm-x", "SECRET-VALUE")
    save_vault_bindings({"demo": VaultBinding("demo", backend="local", sync_mode="direct")})


def test_restored_audit_chain_continues_without_a_duplicate_seq(home, monkeypatch, tmp_path, capsys):
    """`.clock` (the seq counter) MUST travel with audit.log -- restoring
    audit.log alone would reset the counter to 0, so the next append() after
    import could re-mint a seq that already exists in the restored chain,
    breaking the hash-chain invariant `verify()` checks."""
    from portunus import AuditChain, Broker

    _populate_vault()
    broker = Broker(Registry(), AuditChain())
    broker.audit.append("resolve", "sm-x", "ok")
    broker.audit.append("resolve", "sm-x", "ok")
    broker.audit.append("resolve", "sm-x", "ok")
    source_seqs = [e["seq"] for e in AuditChain().entries()]
    assert source_seqs == [1, 2, 3]

    archive = tmp_path / "backup.pvault"
    monkeypatch.setenv("PORTUNUS_EXPORT_PASSPHRASE", "pw")
    main(["vault", "export", "--out", str(archive)])
    capsys.readouterr()

    import_home = tmp_path / "import-home"
    monkeypatch.setenv("PORTUNUS_HOME", str(import_home))
    rc = main(["vault", "import", str(archive), "--force"])
    assert rc == 0
    capsys.readouterr()

    # The import itself appends its own "vault_import" entry (seq 4) against
    # the just-restored chain -- continuing it, not colliding with it.
    restored = AuditChain()
    entries = restored.entries()
    seqs = [e["seq"] for e in entries]
    assert seqs == sorted(seqs), f"non-monotonic seq after restore: {seqs}"
    assert len(seqs) == len(set(seqs)), f"duplicate seq after restore: {seqs}"
    assert restored.verify() is True

    # One more real append after the restore must extend the chain cleanly.
    broker2 = Broker(Registry(), AuditChain())
    broker2.audit.append("resolve", "sm-x", "ok")
    final = AuditChain()
    final_seqs = [e["seq"] for e in final.entries()]
    assert final_seqs == sorted(final_seqs)
    assert len(final_seqs) == len(set(final_seqs))
    assert final.verify() is True


def test_export_import_round_trips_byte_identical(home, monkeypatch, tmp_path, capsys):
    _populate_vault()
    archive = tmp_path / "backup.pvault"
    monkeypatch.setenv("PORTUNUS_EXPORT_PASSPHRASE", "correct-horse-battery-staple")

    rc = main(["vault", "export", "--out", str(archive)])
    assert rc == 0
    assert archive.exists()

    import_home = tmp_path / "import-home"
    monkeypatch.setenv("PORTUNUS_HOME", str(import_home))
    rc = main(["vault", "import", str(archive), "--force"])
    assert rc == 0

    for name in ("registry.json", "vault.enc.json", "master.key", "vault-bindings.json"):
        assert (home / name).read_bytes() == (import_home / name).read_bytes()

    # portunus verify / reg json work on the restored vault exactly as they
    # did against the original.
    capsys.readouterr()
    assert main(["verify"]) == 0
    capsys.readouterr()
    assert main(["reg", "json"]) == 0
    data = json.loads(capsys.readouterr().out)
    assert "x" in data
    assert LocalEncryptedBackend().access("sm-x") == "SECRET-VALUE"


def test_import_wrong_passphrase_fails_closed_no_partial_write(home, monkeypatch, tmp_path, capsys):
    _populate_vault()
    archive = tmp_path / "backup.pvault"
    monkeypatch.setenv("PORTUNUS_EXPORT_PASSPHRASE", "correct-passphrase")
    main(["vault", "export", "--out", str(archive)])
    capsys.readouterr()

    import_home = tmp_path / "import-home"
    monkeypatch.setenv("PORTUNUS_HOME", str(import_home))
    monkeypatch.setenv("PORTUNUS_EXPORT_PASSPHRASE", "wrong-passphrase")

    rc = main(["vault", "import", str(archive), "--force"])
    err = capsys.readouterr().err
    assert rc != 0
    assert "passphrase" in err.lower()
    assert not import_home.exists() or list(import_home.iterdir()) == []


def test_import_refuses_non_empty_target_without_force(home, monkeypatch, tmp_path, capsys):
    _populate_vault()
    archive = tmp_path / "backup.pvault"
    monkeypatch.setenv("PORTUNUS_EXPORT_PASSPHRASE", "correct-passphrase")
    main(["vault", "export", "--out", str(archive)])
    capsys.readouterr()

    import_home = tmp_path / "import-home"
    import_home.mkdir()
    (import_home / "registry.json").write_text("{}")
    monkeypatch.setenv("PORTUNUS_HOME", str(import_home))

    rc = main(["vault", "import", str(archive)])  # no --force
    err = capsys.readouterr().err
    assert rc != 0
    assert "force" in err.lower()
    assert (import_home / "registry.json").read_text() == "{}"  # untouched


def test_import_succeeds_with_force_against_a_non_empty_target(home, monkeypatch, tmp_path, capsys):
    _populate_vault()
    archive = tmp_path / "backup.pvault"
    monkeypatch.setenv("PORTUNUS_EXPORT_PASSPHRASE", "correct-passphrase")
    main(["vault", "export", "--out", str(archive)])
    capsys.readouterr()

    import_home = tmp_path / "import-home"
    import_home.mkdir()
    (import_home / "registry.json").write_text("{}")
    monkeypatch.setenv("PORTUNUS_HOME", str(import_home))

    rc = main(["vault", "import", str(archive), "--force"])
    assert rc == 0
    assert Registry().require("x").sm_name == "sm-x"


def test_env_var_passphrase_is_used_over_a_prompt(home, monkeypatch, tmp_path, capsys):
    """PORTUNUS_EXPORT_PASSPHRASE short-circuits the interactive getpass
    prompt entirely -- if it didn't, this test would hang waiting on stdin."""
    _populate_vault()
    archive = tmp_path / "backup.pvault"
    monkeypatch.setenv("PORTUNUS_EXPORT_PASSPHRASE", "from-env-var")
    rc = main(["vault", "export", "--out", str(archive)])
    assert rc == 0

    import_home = tmp_path / "import-home"
    monkeypatch.setenv("PORTUNUS_HOME", str(import_home))
    rc = main(["vault", "import", str(archive), "--force"])
    assert rc == 0


def test_vault_export_import_never_writes_the_passphrase_to_the_audit_log(home, monkeypatch, tmp_path, capsys):
    _populate_vault()
    archive = tmp_path / "backup.pvault"
    monkeypatch.setenv("PORTUNUS_EXPORT_PASSPHRASE", "super-secret-passphrase-marker")
    main(["vault", "export", "--out", str(archive)])
    assert "super-secret-passphrase-marker" not in (home / "audit.log").read_text()

    import_home = tmp_path / "import-home"
    monkeypatch.setenv("PORTUNUS_HOME", str(import_home))
    main(["vault", "import", str(archive), "--force"])
    assert "super-secret-passphrase-marker" not in (import_home / "audit.log").read_text()


def test_vault_export_rejects_an_inline_passphrase_flag():
    """The passphrase is never accepted via an inline CLI flag -- only the
    env var or an interactive prompt (matches `portunus drop`'s own
    boundary-only convention). No such flag exists at all."""
    with pytest.raises(SystemExit):
        main(["vault", "export", "--passphrase", "x"])


def test_vault_import_rejects_an_inline_passphrase_flag(tmp_path):
    with pytest.raises(SystemExit):
        main(["vault", "import", str(tmp_path / "x.pvault"), "--passphrase", "x"])


def test_export_includes_legacy_gcp_bindings_json_when_present(home, monkeypatch, tmp_path):
    Registry().add("x", "sm-x", project="demo")
    (home / "gcp-bindings.json").write_text(json.dumps({"demo": {"wif_audience": "", "account": ""}}))
    archive = tmp_path / "backup.pvault"
    monkeypatch.setenv("PORTUNUS_EXPORT_PASSPHRASE", "pw")
    main(["vault", "export", "--out", str(archive)])

    import_home = tmp_path / "import-home"
    monkeypatch.setenv("PORTUNUS_HOME", str(import_home))
    main(["vault", "import", str(archive), "--force"])
    assert (import_home / "gcp-bindings.json").exists()


def test_export_without_legacy_gcp_bindings_is_not_an_error(home, monkeypatch, tmp_path):
    Registry().add("x", "sm-x", project="demo")
    archive = tmp_path / "backup.pvault"
    monkeypatch.setenv("PORTUNUS_EXPORT_PASSPHRASE", "pw")
    rc = main(["vault", "export", "--out", str(archive)])
    assert rc == 0


def test_archive_is_opaque_without_the_passphrase(home, tmp_path):
    Registry().add("x", "sm-x", project="demo")
    LocalEncryptedBackend().store("sm-x", "TOP-SECRET-VALUE")
    archive_path = export_archive(tmp_path / "a.pvault", "correct-pw")
    raw = archive_path.read_bytes()
    assert b"TOP-SECRET-VALUE" not in raw
    assert b"correct-pw" not in raw


def test_master_key_never_appears_un_re_encrypted_in_the_archive(home, tmp_path):
    """The literal claim design-discussion.md §2 makes: master.key alone is
    enough to decrypt every stored value, so it must never sit in the
    archive un-re-encrypted -- not as a substring anywhere in the raw
    archive bytes, not even base64-transcoded (checked in both encodings)."""
    import base64

    Registry().add("x", "sm-x", project="demo")
    LocalEncryptedBackend().store("sm-x", "TOP-SECRET-VALUE")
    master_key_bytes = (home / "master.key").read_bytes()

    archive_path = export_archive(tmp_path / "a.pvault", "correct-pw")
    raw = archive_path.read_bytes()

    assert master_key_bytes not in raw
    assert base64.b64encode(master_key_bytes) not in raw


def test_export_requires_a_nonempty_passphrase(home, tmp_path):
    with pytest.raises(ExportError):
        export_archive(tmp_path / "a.pvault", "")


def test_import_requires_a_nonempty_passphrase(home, tmp_path):
    with pytest.raises(ExportError):
        import_archive(tmp_path / "nonexistent.pvault", "")
