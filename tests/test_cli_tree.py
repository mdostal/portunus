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


# --- story 03 (portunus-provenance-graph): --by {group,repo} ---

def _seed_repo(home):
    reg = Registry()
    reg.add("a", "sm-a", repo="event-api", tags={"key": "sm-a"})
    reg.add("b", "sm-b", repo="event-api", tags={"key": "sm-b"})
    reg.add("c", "sm-c", repo="social-engine", tags={"key": "sm-c"})
    reg.add("d", "sm-d")  # no repo set
    return reg


def test_tree_default_by_is_unchanged_from_before_this_story(home, capsys):
    _seed(home)
    rc = main(["tree"])
    out_no_flag = capsys.readouterr().out
    assert rc == 0
    rc = main(["tree", "--by", "group"])
    out_explicit_group = capsys.readouterr().out
    assert rc == 0
    assert out_no_flag == out_explicit_group


def test_tree_by_repo_nests_under_repo_field(home, capsys):
    _seed_repo(home)
    rc = main(["tree", "--by", "repo"])
    out = capsys.readouterr().out
    assert rc == 0
    assert "event-api" in out
    assert "social-engine" in out
    assert "a" in out and "b" in out and "c" in out


def test_tree_by_repo_no_repo_set_bucket_never_drops_a_reference(home, capsys):
    _seed_repo(home)
    rc = main(["tree", "--by", "repo", "--json"])
    out = json.loads(capsys.readouterr().out)
    assert rc == 0
    assert "d" in out["ungrouped"]


def test_tree_by_repo_json_matches_cli_shape(home, capsys):
    _seed_repo(home)
    rc = main(["tree", "--by", "repo", "--json"])
    out = json.loads(capsys.readouterr().out)
    assert rc == 0
    assert "event-api" in out["tree"]
    assert sorted(out["tree"]["event-api"]["_refs"]) == ["a", "b"]


def test_tree_by_invalid_value_rejected(home, capsys):
    import pytest
    with pytest.raises(SystemExit):
        main(["tree", "--by", "nonsense"])


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
