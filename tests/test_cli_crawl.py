"""`portunus crawl` (portunus-metadata-crawl Slice 1)."""
import json

from portunus import Registry
from portunus.cli import main


def test_crawl_lists_incomplete_references(home, capsys):
    Registry().add("x", "SHINDIG_API_KEY", org="firefly-events", project="shindig")
    rc = main(["crawl", "--json"])
    out = capsys.readouterr().out
    assert rc == 0
    data = json.loads(out)
    assert len(data) == 1
    assert data[0]["name"] == "x"
    assert data[0]["sm_name"] == "SHINDIG_API_KEY"


def test_crawl_excludes_complete_references(home, capsys):
    Registry().add(
        "x", "sm-x", org="firefly-events", project="shindig",
        description="a key", purpose="testing",
    )
    rc = main(["crawl", "--json"])
    data = json.loads(capsys.readouterr().out)
    assert rc == 0
    assert data == []


def test_crawl_scopes_by_org(home, capsys):
    Registry().add("a", "sm-a", org="firefly-events", project="shindig")
    Registry().add("b", "sm-b", org="other-org", project="gig-tracker")
    rc = main(["crawl", "--org", "firefly-events", "--json"])
    data = json.loads(capsys.readouterr().out)
    assert rc == 0
    assert [c["name"] for c in data] == ["a"]


def test_crawl_no_candidates_reports_cleanly(home, capsys):
    rc = main(["crawl"])
    out = capsys.readouterr().out
    assert rc == 0
    assert "no candidates" in out.lower()


def test_crawl_never_leaks_a_value_source_check():
    import ast
    import inspect
    import textwrap
    from portunus.cli import cmd_crawl

    src = textwrap.dedent(inspect.getsource(cmd_crawl))
    tree = ast.parse(src)
    code = ast.unparse(tree)
    assert ".access(" not in code
