"""`portunus vault status` -- first-run detection for the setup wizard
(portunus-vault-trust-and-access Slice 8)."""
import json

from portunus import Registry
from portunus.backend import VaultBinding, save_vault_bindings
from portunus.cli import main


def test_fresh_home_reports_not_initialized(home, capsys):
    rc = main(["vault", "status", "--json"])
    out = capsys.readouterr().out
    assert rc == 0
    assert json.loads(out) == {"initialized": False}


def test_a_single_reference_marks_it_initialized(home, capsys):
    Registry().add("x", "sm-x")
    rc = main(["vault", "status", "--json"])
    data = json.loads(capsys.readouterr().out)
    assert rc == 0
    assert data == {"initialized": True}


def test_a_single_binding_marks_it_initialized_even_with_no_references(home, capsys):
    save_vault_bindings({"demo": VaultBinding("demo")})
    rc = main(["vault", "status", "--json"])
    data = json.loads(capsys.readouterr().out)
    assert rc == 0
    assert data == {"initialized": True}
