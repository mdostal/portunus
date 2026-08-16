"""`portunus reg rm` -- real gap found while investigating a user's live
vault: it only ever removed the registry pointer, never the underlying
encrypted value, leaving an orphan in the local-encrypted vault forever."""
from portunus import Registry
from portunus.cli import main
from portunus.localvault import LocalEncryptedBackend


def test_reg_rm_removes_the_registry_entry(home, capsys):
    Registry().add("x", "sm-x")
    rc = main(["reg", "rm", "x"])
    out = capsys.readouterr().out
    assert rc == 0
    assert "x" not in {r.name for r in Registry()}
    assert "removed" in out


def test_reg_rm_also_purges_the_local_encrypted_value(home, capsys):
    Registry().add("x", "sm-x")
    LocalEncryptedBackend().store("sm-x", "some-value-1234567890")

    main(["reg", "rm", "x"])
    capsys.readouterr()

    # the underlying encrypted value must be gone too, not orphaned
    backend = LocalEncryptedBackend()
    import pytest
    from portunus.backend import BackendError

    with pytest.raises(BackendError):
        backend.access("sm-x")


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
    tree = ast.parse(src)
    code = ast.unparse(tree)
    # the function may call backend.access implicitly via other actions
    # (it doesn't), but must never print/return a decrypted value
    assert "print(value" not in code
    assert "return value" not in code
