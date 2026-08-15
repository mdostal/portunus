"""`--org` across reg add / retag / retag-bulk / drop (portunus-vault-trust-
and-access, Slice 1) -- the CLI surface for the org field."""
import json

from portunus import Registry
from portunus.cli import main


def test_reg_add_accepts_org(home, capsys):
    rc = main(["reg", "add", "x", "sm-x", "--org", "firefly-events", "--project", "shindig"])
    assert rc == 0
    capsys.readouterr()
    assert Registry().require("x").org == "firefly-events"


def test_retag_accepts_org(home, capsys):
    Registry().add("x", "sm-x", project="shindig")
    rc = main(["retag", "x", "--org", "firefly-events"])
    out = capsys.readouterr().out
    assert rc == 0
    assert "org=firefly-events" in out
    assert Registry().require("x").org == "firefly-events"


def test_retag_bulk_backfills_org_by_group_prefix(home, capsys):
    reg = Registry()
    reg.add("a", "sm-a", group="shindig/api", project="shindig-api")
    reg.add("b", "sm-b", group="shindig/web", project="shindig-web")
    reg.add("c", "sm-c", group="other/service", project="other-service")

    rc = main(["retag-bulk", "--group-prefix", "shindig", "--org", "firefly-events", "--json"])
    out = capsys.readouterr().out
    assert rc == 0
    data = json.loads(out)
    assert set(data["updated"]) == {"a", "b"}
    assert Registry().require("a").org == "firefly-events"
    assert Registry().require("b").org == "firefly-events"
    assert Registry().require("c").org == ""


def test_drop_accepts_org(home, monkeypatch, capsys):
    monkeypatch.delenv("PORTUNUS_BACKEND", raising=False)
    value_file = home / "value.txt"
    value_file.write_text("s3kr3t\n")

    rc = main([
        "drop", "shindig-api-key", "sm-shindig-api-key",
        "--value-file", str(value_file), "--org", "firefly-events",
        "--repo", "shindig", "--source-files", "docker-compose.yml,shindig.env",
    ])
    assert rc == 0
    capsys.readouterr()
    ref = Registry().require("shindig-api-key")
    assert ref.org == "firefly-events"
    assert ref.repo == "shindig"
    assert ref.source_files == ["docker-compose.yml", "shindig.env"]


def test_find_by_org_tag(home, capsys):
    Registry().add("x", "sm-x", org="firefly-events", project="shindig")
    Registry().add("y", "sm-y", org="other-org", project="gig-tracker")

    rc = main(["find", "--tags", "org=firefly-events"])
    out = capsys.readouterr().out
    assert rc == 0
    assert "x" in out
    assert "y" not in out
