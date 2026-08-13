"""portunus tree [--project X] [--json] (story 03, portunus-secret-tree).

Ungrouped references must never be silently dropped (Grill H1) -- the real
vault has 382 currently-ungrouped references as of this session, so this is
the default case, not an edge case."""
import json

from portunus import Registry
from portunus.cli import main


def _seed(home):
    reg = Registry()
    reg.add("staging-supabase-auth", "sm-a", project="project-y",
            group="project-y/supabase/auth", related=["project-y-mongodb-prod"])
    reg.add("project-y-mongodb-prod", "sm-b", project="project-y", group="project-y/mongodb")
    reg.add("no-group-ref", "sm-c", project="project-y")
    return reg


def test_tree_shows_nested_group_and_ungrouped_bucket(home, capsys):
    _seed(home)
    rc = main(["tree"])
    out = capsys.readouterr().out
    assert rc == 0
    assert "project-y" in out
    assert "supabase" in out
    assert "auth" in out
    assert "staging-supabase-auth" in out
    assert "(ungrouped)" in out
    assert "no-group-ref" in out


def test_tree_project_filter_only_includes_matching_project(home, capsys):
    _seed(home)
    reg = Registry()
    reg.add("other-project-ref", "sm-d", project="other-project", group="somewhere")
    rc = main(["tree", "--project", "project-y"])
    out = capsys.readouterr().out
    assert rc == 0
    assert "staging-supabase-auth" in out
    assert "other-project-ref" not in out


def test_tree_shows_related_link_when_target_present(home, capsys):
    _seed(home)
    rc = main(["tree"])
    out = capsys.readouterr().out
    assert "project-y-mongodb-prod" in out
    # the related annotation shows up near staging-supabase-auth's leaf
    assert "related" in out.lower()


def test_tree_marks_unresolved_related(home, capsys):
    reg = Registry()
    reg.add("x", "sm-x", related=["nonexistent-ref"])
    rc = main(["tree"])
    out = capsys.readouterr().out
    assert rc == 0
    assert "(unresolved)" in out


def test_tree_json_shape(home, capsys):
    _seed(home)
    rc = main(["tree", "--json"])
    out = capsys.readouterr().out
    assert rc == 0
    data = json.loads(out)
    assert "ungrouped" in data
    assert "no-group-ref" in data["ungrouped"]
    assert "tree" in data
    assert "project-y" in data["tree"]
    assert "supabase" in data["tree"]["project-y"]
    assert "auth" in data["tree"]["project-y"]["supabase"]
    assert "staging-supabase-auth" in data["tree"]["project-y"]["supabase"]["auth"]["_refs"]
    assert "refs" in data
    assert data["refs"]["staging-supabase-auth"]["related"][0]["name"] == "project-y-mongodb-prod"
    assert data["refs"]["staging-supabase-auth"]["related"][0]["unresolved"] is False


def test_tree_json_unresolved_related_marked(home, capsys):
    reg = Registry()
    reg.add("x", "sm-x", related=["nonexistent-ref"])
    rc = main(["tree", "--json"])
    out = capsys.readouterr().out
    data = json.loads(out)
    assert data["refs"]["x"]["related"][0]["unresolved"] is True


def test_tree_empty_registry_prints_message_not_error(home, capsys):
    rc = main(["tree"])
    out = capsys.readouterr().out
    assert rc == 0
    assert "no references" in out.lower() or "empty" in out.lower()


def test_tree_never_touches_a_backend():
    """Structural: tree-building is a pure metadata read, same discipline as
    list_by_project()/discover.py."""
    import ast
    import inspect
    import textwrap
    from portunus import cli
    src = textwrap.dedent(inspect.getsource(cli.cmd_tree))
    tree = ast.parse(src)
    func = tree.body[0]
    body = func.body[1:] if ast.get_docstring(func) else func.body
    code_src = "\n".join(ast.unparse(node) for node in body)
    assert ".access(" not in code_src
