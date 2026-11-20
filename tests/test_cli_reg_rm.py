"""`portunus reg rm` -- removes the registry pointer only by default.

History: rm first left the encrypted value orphaned, then was changed to
always purge it, which silently destroyed a value other references sharing
the same backend + sm_name still resolved to (portunus#247 / PANT-1407).
Purging is now opt-in via --purge-value and refused while shared."""
import pytest

from portunus import Registry
from portunus.backend import BackendError, VaultBinding, save_vault_bindings
from portunus.cli import main
from portunus.localvault import LocalEncryptedBackend


def _gone(sm_name):
    with pytest.raises(BackendError):
        LocalEncryptedBackend().access(sm_name)


def test_reg_rm_removes_the_registry_entry(home, capsys):
    Registry().add("x", "sm-x")
    rc = main(["reg", "rm", "x"])
    out = capsys.readouterr().out
    assert rc == 0
    assert "x" not in {r.name for r in Registry()}
    assert "removed" in out


def test_reg_rm_keeps_the_stored_value_by_default(home, capsys):
    Registry().add("x", "sm-x")
    LocalEncryptedBackend().store("sm-x", "some-value-1234567890")

    rc = main(["reg", "rm", "x"])
    out = capsys.readouterr().out

    assert rc == 0
    assert "registry entry only" in out
    assert "--purge-value" in out
    assert LocalEncryptedBackend().access("sm-x") == "some-value-1234567890"


def test_reg_rm_purge_value_purges_an_unshared_local_value(home, capsys):
    Registry().add("x", "sm-x")
    LocalEncryptedBackend().store("sm-x", "some-value-1234567890")

    rc = main(["reg", "rm", "x", "--purge-value"])
    out = capsys.readouterr().out

    assert rc == 0
    assert "both purged" in out
    assert "x" not in {r.name for r in Registry()}
    _gone("sm-x")


def test_reg_rm_purge_value_refuses_while_another_ref_shares_the_value(home, capsys):
    reg = Registry()
    reg.add("orig", "STRIPE_SECRET_KEY")
    reg.add("tmp-check", "STRIPE_SECRET_KEY", project="local-check-tmp")
    LocalEncryptedBackend().store("STRIPE_SECRET_KEY", "sk_live_value_1234567890")

    rc = main(["reg", "rm", "tmp-check", "--purge-value"])
    captured = capsys.readouterr()

    assert rc != 0
    assert "refusing" in captured.err
    assert "orig" in captured.err
    # nothing removed: neither the entry nor the shared value
    assert {"orig", "tmp-check"} <= {r.name for r in Registry()}
    assert LocalEncryptedBackend().access("STRIPE_SECRET_KEY") == "sk_live_value_1234567890"


def test_reg_rm_issue_repro_no_longer_loses_the_shared_value(home, capsys):
    """Exact repro from portunus#247: the original ref is bound to GCP, a
    temp ref under an unbound project reads the local copy, then rm."""
    save_vault_bindings({"personalsites-487021": VaultBinding("personalsites-487021", backend="gcp")})
    reg = Registry()
    reg.add("personalsites-487021-stripe_secret_key", "STRIPE_SECRET_KEY",
            project="personalsites-487021")
    reg.add("tmp-check", "STRIPE_SECRET_KEY", project="local-check-tmp")
    LocalEncryptedBackend().store("STRIPE_SECRET_KEY", "sk_live_value_1234567890")

    assert main(["reg", "rm", "tmp-check"]) == 0
    capsys.readouterr()
    assert LocalEncryptedBackend().access("STRIPE_SECRET_KEY") == "sk_live_value_1234567890"


def test_reg_rm_purge_refuses_when_a_gcp_ref_falls_back_to_the_same_local_value(home, capsys):
    # Since #246 (PANT-1408) every GCP-routed reference reads the local vault
    # on a remote NOT_FOUND (LocalFallbackBackend), so a GCP ref with the same
    # sm_name DOES share the local value: purging it would break cloud-ref's
    # fallback. The guard must refuse, and nothing is removed.
    save_vault_bindings({"cloudy": VaultBinding("cloudy", backend="gcp")})
    reg = Registry()
    reg.add("cloud-ref", "sm-x", project="cloudy")
    reg.add("local-ref", "sm-x")
    LocalEncryptedBackend().store("sm-x", "some-value-1234567890")

    rc = main(["reg", "rm", "local-ref", "--purge-value"])
    err = capsys.readouterr().err

    assert rc == 1
    assert "cloud-ref" in err
    assert LocalEncryptedBackend().access("sm-x") == "some-value-1234567890"
    assert {"cloud-ref", "local-ref"} <= {r.name for r in Registry()}


def test_reg_add_warns_when_sm_name_is_shared_in_the_same_backend(home, capsys):
    assert main(["reg", "add", "a", "sm-shared"]) == 0
    capsys.readouterr()

    assert main(["reg", "add", "b", "sm-shared", "--project", "other"]) == 0
    captured = capsys.readouterr()

    assert "registered" in captured.out
    assert "warning" in captured.err
    assert "a" in captured.err and "sm-shared" in captured.err


def test_reg_add_does_not_warn_for_a_unique_sm_name(home, capsys):
    main(["reg", "add", "a", "sm-a"])
    main(["reg", "add", "b", "sm-b"])
    assert "warning" not in capsys.readouterr().err


def test_reg_add_does_not_warn_when_re_adding_the_same_name(home, capsys):
    main(["reg", "add", "a", "sm-a"])
    main(["reg", "add", "a", "sm-a", "--description", "updated"])
    assert "warning" not in capsys.readouterr().err


def test_reg_rm_on_unknown_reference_is_a_harmless_no_op(home, capsys):
    rc = main(["reg", "rm", "does-not-exist"])
    out = capsys.readouterr().out
    assert rc == 0
    assert "no such reference" in out


def test_reg_rm_never_prints_the_value_source_check():
    import ast
    import inspect

    from portunus.cli import cmd_reg

    src = inspect.getsource(cmd_reg)
    code = ast.unparse(ast.parse(src))
    assert "print(value" not in code
    assert "return value" not in code
