"""portunus list --project <id> and `portunus ask "what secrets are available..."` (story 05)."""
import json

from portunus import Registry
from portunus.cli import main


def test_cli_list_prints_metadata_only(home, capsys):
    reg = Registry()
    reg.add("a", "sm-a", provider="gcp", project="demo-project-483920",
            description="Auth secret", purpose="Session signing")

    rc = main(["list", "--project", "demo-project-483920"])
    out = capsys.readouterr().out
    assert rc == 0
    assert "sm-a" in out
    assert "Auth secret" in out
    assert "Session signing" in out


def test_cli_list_json_output(home, capsys):
    reg = Registry()
    reg.add("a", "sm-a", provider="gcp", project="p")
    rc = main(["list", "--project", "p", "--json"])
    out = capsys.readouterr().out
    assert rc == 0
    data = json.loads(out)
    assert data[0]["sm_name"] == "sm-a"
    assert "value" not in data[0]


def test_cli_list_empty_project_is_not_an_error(home, capsys):
    rc = main(["list", "--project", "nonexistent"])
    out = capsys.readouterr().out
    assert rc == 0
    assert "no references found" in out


def test_ask_list_intent_routes_to_list_by_project(home, capsys):
    reg = Registry()
    reg.add("a", "sm-a", provider="vercel", project="mdostal.com", description="prod key")

    rc = main(["ask", "what secrets are available for mdostal.com"])
    out = capsys.readouterr().out
    assert rc == 0
    assert "sm-a" in out
    assert "prod key" in out


def test_ask_list_without_recognizable_project_fails_closed(home, capsys):
    rc = main(["ask", "list secrets"])
    err = capsys.readouterr().err
    assert rc == 1
    assert "project" in err
