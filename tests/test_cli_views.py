"""`portunus views create/add/remove/delete/show` (portunus-vault-trust-and-
access Slice 4)."""
import json

from portunus.cli import main


def test_views_create_add_show(home, capsys):
    rc = main(["views", "create", "shindig-deploy", "--description", "everything for the deploy"])
    assert rc == 0
    capsys.readouterr()

    rc = main(["views", "add", "shindig-deploy", "shindig-api-key"])
    assert rc == 0
    capsys.readouterr()

    rc = main(["views", "add", "shindig-deploy", "shindig-db-url"])
    assert rc == 0
    capsys.readouterr()

    rc = main(["views", "show", "shindig-deploy", "--json"])
    out = capsys.readouterr().out
    assert rc == 0
    data = json.loads(out)
    assert data["shindig-deploy"]["description"] == "everything for the deploy"
    assert data["shindig-deploy"]["ref_names"] == ["shindig-api-key", "shindig-db-url"]


def test_views_add_to_unknown_view_fails_clearly(home, capsys):
    rc = main(["views", "add", "nonexistent", "some-ref"])
    err = capsys.readouterr().err
    assert rc != 0
    assert "nonexistent" in err


def test_views_remove(home, capsys):
    main(["views", "create", "shindig-deploy"])
    main(["views", "add", "shindig-deploy", "a"])
    main(["views", "add", "shindig-deploy", "b"])
    capsys.readouterr()

    rc = main(["views", "remove", "shindig-deploy", "a"])
    assert rc == 0
    capsys.readouterr()

    rc = main(["views", "show", "shindig-deploy", "--json"])
    data = json.loads(capsys.readouterr().out)
    assert data["shindig-deploy"]["ref_names"] == ["b"]


def test_views_delete(home, capsys):
    main(["views", "create", "shindig-deploy"])
    capsys.readouterr()

    rc = main(["views", "delete", "shindig-deploy"])
    out = capsys.readouterr().out
    assert rc == 0
    assert "deleted" in out

    rc = main(["views", "show", "--json"])
    data = json.loads(capsys.readouterr().out)
    assert data == {}


def test_views_show_all(home, capsys):
    main(["views", "create", "view-a"])
    main(["views", "create", "view-b"])
    capsys.readouterr()

    rc = main(["views", "show", "--json"])
    data = json.loads(capsys.readouterr().out)
    assert rc == 0
    assert set(data.keys()) == {"view-a", "view-b"}


def test_views_never_leaks_a_value_source_check():
    """AST-level: no cmd_views_* handler references a variable that could
    hold a secret value -- views only ever carry reference names."""
    import ast
    import inspect
    import textwrap
    from portunus import cli

    for fn_name in ("cmd_views_create", "cmd_views_add", "cmd_views_remove", "cmd_views_show"):
        src = textwrap.dedent(inspect.getsource(getattr(cli, fn_name)))
        tree = ast.parse(src)
        code = ast.unparse(tree)
        assert ".access(" not in code
