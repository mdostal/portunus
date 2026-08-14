"""portunus rotation-bindings set/show (story 02, portunus-metadata-and-
rotation-provenance). Same round-trip shape as test_cli_bindings.py's
VaultBinding coverage -- the rotation-provenance analog."""
import json

from portunus.cli import main
from portunus.rotation import load_rotation_bindings


def test_rotation_bindings_set_writes_account(home, capsys):
    rc = main(["rotation-bindings", "set", "vercel", "--account", "my-team-slug"])
    assert rc == 0
    bindings = load_rotation_bindings()
    assert bindings["vercel"].account == "my-team-slug"


def test_rotation_bindings_set_defaults_status_to_stub(home, capsys):
    rc = main(["rotation-bindings", "set", "vercel", "--account", "my-team-slug"])
    assert rc == 0
    bindings = load_rotation_bindings()
    assert bindings["vercel"].status == "stub"


def test_rotation_bindings_set_is_an_upsert_preserving_account(home, capsys):
    main(["rotation-bindings", "set", "vercel", "--account", "my-team-slug"])
    capsys.readouterr()
    rc = main(["rotation-bindings", "set", "vercel", "--status", "stub"])
    assert rc == 0
    bindings = load_rotation_bindings()
    assert bindings["vercel"].account == "my-team-slug"


def test_rotation_bindings_show_one_provider(home, capsys):
    main(["rotation-bindings", "set", "vercel", "--account", "my-team-slug"])
    capsys.readouterr()
    rc = main(["rotation-bindings", "show", "vercel"])
    out = capsys.readouterr().out
    assert rc == 0
    assert "my-team-slug" in out
    assert "stub" in out


def test_rotation_bindings_show_all(home, capsys):
    main(["rotation-bindings", "set", "vercel", "--account", "v-team"])
    capsys.readouterr()
    main(["rotation-bindings", "set", "github", "--account", "gh-org"])
    capsys.readouterr()
    rc = main(["rotation-bindings", "show"])
    out = capsys.readouterr().out
    assert rc == 0
    assert "vercel" in out and "v-team" in out
    assert "github" in out and "gh-org" in out


def test_rotation_bindings_show_missing_provider_reports_cleanly(home, capsys):
    rc = main(["rotation-bindings", "show", "nonexistent"])
    out = capsys.readouterr().out
    assert rc == 0
    assert "no rotation binding" in out.lower()


def test_rotation_bindings_show_json(home, capsys):
    main(["rotation-bindings", "set", "vercel", "--account", "my-team-slug"])
    capsys.readouterr()
    rc = main(["rotation-bindings", "show", "vercel", "--json"])
    out = capsys.readouterr().out
    assert rc == 0
    data = json.loads(out)
    assert data["vercel"]["account"] == "my-team-slug"
    assert data["vercel"]["status"] == "stub"
