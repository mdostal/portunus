"""portunus retag-bulk --group-prefix (story 02, portunus-provenance-graph).
Real unlock for backfilling repo/source_files across many already-grouped
references at once instead of one `retag` call per reference."""
import json

from portunus.cli import main
from portunus import Registry


def _seed(home):
    """Matches the real ffe-cicd data's shape: every reference already
    carries a unique tags{} entry (its raw SM name) even though provider/
    project/env are often shared -- that per-entry uniqueness is what keeps
    a bulk repo backfill from colliding two different secrets into the same
    structured-tag identity. A reference with no distinguishing tag at all
    would legitimately collide once every other structured field it shares
    with a sibling becomes identical too -- see the dedicated collision test
    below, which constructs that case on purpose rather than by accident."""
    reg = Registry()
    reg.add("a", "sm-a", group="ffe-cicd/event-api/prod", tags={"key": "sm-a"})
    reg.add("b", "sm-b", group="ffe-cicd/event-api/dev", tags={"key": "sm-b"})
    reg.add("c", "sm-c", group="ffe-cicd/social-engine/prod", tags={"key": "sm-c"})
    reg.add("d", "sm-d", group="")  # no group -- must never match any prefix
    return reg


def test_retag_bulk_updates_only_matching_prefix(home, capsys):
    _seed(home)
    rc = main(["retag-bulk", "--group-prefix", "ffe-cicd/event-api", "--repo", "event-api"])
    assert rc == 0
    reg = Registry()
    assert reg.require("a").repo == "event-api"
    assert reg.require("b").repo == "event-api"
    assert reg.require("c").repo == ""
    assert reg.require("d").repo == ""


def test_retag_bulk_dry_run_makes_zero_writes(home, capsys):
    _seed(home)
    rc = main(["retag-bulk", "--group-prefix", "ffe-cicd/event-api", "--repo", "event-api", "--dry-run"])
    assert rc == 0
    reg = Registry()
    assert reg.require("a").repo == ""
    assert reg.require("b").repo == ""


def test_retag_bulk_dry_run_reports_what_would_change(home, capsys):
    _seed(home)
    rc = main(["retag-bulk", "--group-prefix", "ffe-cicd/event-api", "--repo", "event-api",
               "--dry-run", "--json"])
    assert rc == 0
    out = json.loads(capsys.readouterr().out)
    assert sorted(out["would_update"]) == ["a", "b"]


def test_retag_bulk_json_reports_updated(home, capsys):
    _seed(home)
    rc = main(["retag-bulk", "--group-prefix", "ffe-cicd/event-api", "--repo", "event-api", "--json"])
    assert rc == 0
    out = json.loads(capsys.readouterr().out)
    assert sorted(out["updated"]) == ["a", "b"]
    assert out["failed"] == []


def test_retag_bulk_zero_matches_is_not_an_error(home, capsys):
    _seed(home)
    rc = main(["retag-bulk", "--group-prefix", "nonexistent/prefix", "--repo", "x", "--json"])
    assert rc == 0
    out = json.loads(capsys.readouterr().out)
    assert out["updated"] == []
    assert out["failed"] == []


def test_retag_bulk_one_collision_does_not_abort_the_rest(home, capsys):
    reg = Registry()
    # a and b share provider/project with no other distinguishing tag --
    # once both get the same repo, they become genuinely indistinguishable
    # by structured tags (a real, correct collision, not a bug). c carries
    # its own distinguishing tag (matching the real ffe-cicd data's shape),
    # so it updates cleanly regardless of what happens to a/b.
    reg.add("a", "sm-a", group="g/x", provider="gcp", project="p", repo="taken")
    reg.add("b", "sm-b", group="g/x", provider="gcp", project="p")
    reg.add("c", "sm-c", group="g/x", tags={"key": "sm-c"})
    rc = main(["retag-bulk", "--group-prefix", "g/x", "--repo", "taken", "--json"])
    assert rc == 0
    out = json.loads(capsys.readouterr().out)
    assert "c" in out["updated"]
    assert any(f["name"] == "b" for f in out["failed"])


def test_retag_bulk_source_files(home, capsys):
    _seed(home)
    rc = main(["retag-bulk", "--group-prefix", "ffe-cicd/event-api",
               "--source-files", "docker-compose.yml,ci.yml"])
    assert rc == 0
    ref = Registry().require("a")
    assert ref.source_files == ["docker-compose.yml", "ci.yml"]


def test_retag_bulk_requires_group_prefix(home, capsys):
    import pytest
    with pytest.raises(SystemExit):
        main(["retag-bulk", "--repo", "x"])
