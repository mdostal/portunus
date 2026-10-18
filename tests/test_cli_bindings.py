"""portunus bindings set/show (story 04, portunus-gcp-multi-account).

Only way to configure gcp-bindings.json before this story was hand-editing
JSON -- save_vault_bindings() was only ever called by tests."""
import json

from portunus.backend import load_vault_bindings
from portunus.cli import main


def test_bindings_set_writes_account(home, capsys):
    rc = main(["bindings", "set", "demo", "--account", "user@example.com"])
    assert rc == 0
    bindings = load_vault_bindings()
    assert bindings["demo"].account == "user@example.com"


def test_bindings_set_writes_wif_audience(home, capsys):
    rc = main(["bindings", "set", "demo", "--wif-audience", "//iam.googleapis.com/some/aud"])
    assert rc == 0
    bindings = load_vault_bindings()
    assert bindings["demo"].wif_audience == "//iam.googleapis.com/some/aud"


def test_bindings_set_is_an_upsert_preserving_wif_audience(home, capsys):
    main(["bindings", "set", "demo", "--wif-audience", "//iam.googleapis.com/some/aud"])
    capsys.readouterr()
    rc = main(["bindings", "set", "demo", "--account", "user@example.com"])
    assert rc == 0
    bindings = load_vault_bindings()
    assert bindings["demo"].account == "user@example.com"
    assert bindings["demo"].wif_audience == "//iam.googleapis.com/some/aud"


def test_bindings_set_is_an_upsert_preserving_account(home, capsys):
    main(["bindings", "set", "demo", "--account", "user@example.com"])
    capsys.readouterr()
    rc = main(["bindings", "set", "demo", "--wif-audience", "//iam.googleapis.com/some/aud"])
    assert rc == 0
    bindings = load_vault_bindings()
    assert bindings["demo"].account == "user@example.com"
    assert bindings["demo"].wif_audience == "//iam.googleapis.com/some/aud"


def test_bindings_show_one_project_prints_real_values(home, capsys):
    main(["bindings", "set", "demo", "--account", "user@example.com", "--wif-audience", "aud-x"])
    capsys.readouterr()
    rc = main(["bindings", "show", "demo"])
    out = capsys.readouterr().out
    assert rc == 0
    assert "user@example.com" in out
    assert "aud-x" in out


def test_bindings_show_all_lists_every_binding(home, capsys):
    main(["bindings", "set", "demo-a", "--account", "a@example.com"])
    capsys.readouterr()
    main(["bindings", "set", "demo-b", "--account", "b@example.com"])
    capsys.readouterr()
    rc = main(["bindings", "show"])
    out = capsys.readouterr().out
    assert rc == 0
    assert "demo-a" in out and "a@example.com" in out
    assert "demo-b" in out and "b@example.com" in out


def test_bindings_show_missing_project_reports_cleanly_not_a_crash(home, capsys):
    rc = main(["bindings", "show", "nonexistent"])
    out = capsys.readouterr().out
    assert rc == 0
    assert "no binding" in out.lower()


def test_bindings_show_json(home, capsys):
    main(["bindings", "set", "demo", "--account", "user@example.com"])
    capsys.readouterr()
    rc = main(["bindings", "show", "demo", "--json"])
    out = capsys.readouterr().out
    assert rc == 0
    data = json.loads(out)
    assert data["demo"]["account"] == "user@example.com"


# --- story 04 (portunus-vault-routing): --backend/--sync-mode -----------

def test_bindings_set_writes_backend_and_sync_mode(home, capsys):
    rc = main(["bindings", "set", "demo", "--backend", "local", "--sync-mode", "cached"])
    assert rc == 0
    bindings = load_vault_bindings()
    assert bindings["demo"].backend == "local"
    assert bindings["demo"].sync_mode == "cached"


def test_bindings_set_backend_upsert_preserves_account(home, capsys):
    main(["bindings", "set", "demo", "--account", "user@example.com"])
    capsys.readouterr()
    rc = main(["bindings", "set", "demo", "--backend", "gcp", "--sync-mode", "cached"])
    assert rc == 0
    bindings = load_vault_bindings()
    assert bindings["demo"].account == "user@example.com"
    assert bindings["demo"].backend == "gcp"
    assert bindings["demo"].sync_mode == "cached"


def test_bindings_set_defaults_backend_and_sync_mode_when_not_passed(home, capsys):
    rc = main(["bindings", "set", "demo", "--account", "user@example.com"])
    assert rc == 0
    bindings = load_vault_bindings()
    assert bindings["demo"].backend == "gcp"
    assert bindings["demo"].sync_mode == "direct"


def test_bindings_show_reports_backend_and_sync_mode(home, capsys):
    main(["bindings", "set", "demo", "--backend", "local", "--sync-mode", "cached"])
    capsys.readouterr()
    rc = main(["bindings", "show", "demo"])
    out = capsys.readouterr().out
    assert rc == 0
    assert "local" in out
    assert "cached" in out


def test_bindings_show_json_includes_backend_and_sync_mode(home, capsys):
    main(["bindings", "set", "demo", "--backend", "local", "--sync-mode", "cached"])
    capsys.readouterr()
    rc = main(["bindings", "show", "demo", "--json"])
    out = capsys.readouterr().out
    assert rc == 0
    data = json.loads(out)
    assert data["demo"]["backend"] == "local"
    assert data["demo"]["sync_mode"] == "cached"


def test_bindings_set_writes_impersonate_service_account(home, capsys):
    rc = main(["bindings", "set", "ffe-cicd",
               "--impersonate-service-account", "portunus@ffe-cicd.iam.gserviceaccount.com"])
    assert rc == 0
    assert (load_vault_bindings()["ffe-cicd"].impersonate_service_account
            == "portunus@ffe-cicd.iam.gserviceaccount.com")


def test_bindings_set_preserves_impersonate_service_account_on_unrelated_update(home, capsys):
    # Regression: `bindings set` used to rebuild VaultBinding without this
    # field, silently wiping a hand-configured impersonation on any update.
    main(["bindings", "set", "ffe-cicd",
          "--impersonate-service-account", "portunus@ffe-cicd.iam.gserviceaccount.com"])
    capsys.readouterr()
    rc = main(["bindings", "set", "ffe-cicd", "--wif-audience", "//iam.googleapis.com/some/aud"])
    assert rc == 0
    b = load_vault_bindings()["ffe-cicd"]
    assert b.wif_audience == "//iam.googleapis.com/some/aud"
    assert b.impersonate_service_account == "portunus@ffe-cicd.iam.gserviceaccount.com"


def test_bindings_show_includes_impersonate_service_account(home, capsys):
    main(["bindings", "set", "ffe-cicd",
          "--impersonate-service-account", "portunus@ffe-cicd.iam.gserviceaccount.com"])
    capsys.readouterr()
    main(["bindings", "show", "ffe-cicd", "--json"])
    out = json.loads(capsys.readouterr().out)
    assert out["ffe-cicd"]["impersonate_service_account"] == "portunus@ffe-cicd.iam.gserviceaccount.com"
