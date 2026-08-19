"""VaultBinding's backend/sync_mode fields + migration-safe config loading
(story 01, portunus-vault-routing). The real gcp-bindings.json for
demo-project-483920/demo-cicd must keep working with zero manual migration --
see test_load_vault_bindings_reads_the_actual_real_gcp_bindings_content."""
import json

from portunus.backend import VaultBinding, load_vault_bindings, save_vault_bindings


def test_vault_binding_defaults_to_gcp_direct():
    b = VaultBinding(project="p")
    assert b.backend == "gcp"
    assert b.sync_mode == "direct"


def test_save_writes_to_new_vault_bindings_file_not_legacy(home):
    save_vault_bindings({"demo": VaultBinding("demo", backend="local", sync_mode="cached")})
    assert (home / "vault-bindings.json").exists()
    assert not (home / "gcp-bindings.json").exists()


def test_new_file_round_trips_backend_and_sync_mode(home):
    save_vault_bindings({
        "demo": VaultBinding("demo", wif_audience="aud", account="a@example.com",
                              backend="local", sync_mode="cached"),
    })
    bindings = load_vault_bindings()
    assert bindings["demo"].backend == "local"
    assert bindings["demo"].sync_mode == "cached"
    assert bindings["demo"].wif_audience == "aud"
    assert bindings["demo"].account == "a@example.com"


def test_legacy_file_loads_with_gcp_direct_defaults(home):
    """No vault-bindings.json, only the old-schema gcp-bindings.json -- every
    entry must default to backend='gcp', sync_mode='direct': byte-for-byte
    today's real, effective behavior."""
    legacy = home / "gcp-bindings.json"
    legacy.write_text(json.dumps({
        "demo": {"wif_audience": "", "account": "user@example.com"},
    }))
    bindings = load_vault_bindings()
    assert bindings["demo"].backend == "gcp"
    assert bindings["demo"].sync_mode == "direct"
    assert bindings["demo"].account == "user@example.com"


def test_load_vault_bindings_reads_the_actual_real_gcp_bindings_content(home):
    """The exact, real PORTUNUS_HOME/gcp-bindings.json content for this
    session's actual vault (demo-project-483920 + demo-cicd) -- verbatim,
    not a simplified fixture."""
    real_content = {
        "demo-project-483920": {"wif_audience": "", "account": "personal@example.com"},
        "demo-cicd": {"wif_audience": "", "account": "work@example.com"},
    }
    (home / "gcp-bindings.json").write_text(json.dumps(real_content, indent=2))

    bindings = load_vault_bindings()

    assert bindings["demo-project-483920"].backend == "gcp"
    assert bindings["demo-project-483920"].sync_mode == "direct"
    assert bindings["demo-project-483920"].account == "personal@example.com"
    assert bindings["demo-cicd"].backend == "gcp"
    assert bindings["demo-cicd"].sync_mode == "direct"
    assert bindings["demo-cicd"].account == "work@example.com"
    # reading must never write a new file as a side effect
    assert not (home / "vault-bindings.json").exists()


def test_new_file_takes_precedence_over_legacy_file(home):
    (home / "gcp-bindings.json").write_text(json.dumps({
        "demo": {"wif_audience": "", "account": "old@example.com"},
    }))
    save_vault_bindings({"demo": VaultBinding("demo", account="new@example.com", backend="local")})

    bindings = load_vault_bindings()
    assert bindings["demo"].account == "new@example.com"
    assert bindings["demo"].backend == "local"


def test_save_never_touches_the_legacy_file(home):
    legacy = home / "gcp-bindings.json"
    legacy_content = json.dumps({"demo": {"wif_audience": "", "account": "old@example.com"}})
    legacy.write_text(legacy_content)

    save_vault_bindings({"other": VaultBinding("other", account="new@example.com")})

    assert legacy.read_text() == legacy_content


def test_env_fallback_still_defaults_to_gcp_direct(home, monkeypatch):
    monkeypatch.setenv("PORTUNUS_GCP_PROJECT", "demo")
    monkeypatch.setenv("PORTUNUS_GCP_WIF_AUDIENCE", "aud")
    bindings = load_vault_bindings()
    assert bindings["demo"].backend == "gcp"
    assert bindings["demo"].sync_mode == "direct"
