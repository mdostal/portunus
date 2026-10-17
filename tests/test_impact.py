"""portunus impact <ref> -- blast radius of a reference (PANT-1122).

Walks `related` transitively in both directions and collects every
project/repo/file/env the reached references are wired into. Read-only,
metadata-only; suggested related links are shown, never written."""
import json

import pytest

from portunus import Registry
from portunus.cli import main
from portunus.impact import impact, suggest_related


def _seed():
    reg = Registry()
    reg.add("supabase-service-key", "sm-a", project="project-y", env="prod",
            group="project-y/supabase/auth", repo="project-y-api",
            source_files=["docker-compose.prod.yml"],
            injected_as={"prod": "env:SUPABASE_SERVICE_KEY"},
            related=["supabase-jwt-secret"])
    reg.add("supabase-jwt-secret", "sm-b", project="project-y", env="prod",
            related=["edge-fn-key", "gone-ref"])
    reg.add("edge-fn-key", "sm-c", project="edge-fns", repo="edge-fns",
            source_files=[".env.production"])
    # Lists the root as related -- inbound edge, still in the blast radius.
    reg.add("worker-supabase-key", "sm-d", project="worker", env="prod",
            injected_as={"prod": "file:/run/secrets/supabase"},
            related=["supabase-service-key"])
    # Unlinked, but shares the root's repo + source file + group parent.
    reg.add("supabase-anon-key", "sm-e", project="project-y", repo="project-y-api",
            group="project-y/supabase/anon", source_files=["docker-compose.prod.yml"])
    reg.add("unrelated", "sm-f", project="elsewhere")
    return Registry()


def test_impact_walks_related_transitively_and_inbound(home):
    result = impact(_seed(), "supabase-service-key")
    names = [n["name"] for n in result["nodes"]]
    assert names[0] == "supabase-service-key"
    assert set(names) == {
        "supabase-service-key", "supabase-jwt-secret", "edge-fn-key", "worker-supabase-key",
    }
    by_name = {n["name"]: n for n in result["nodes"]}
    assert by_name["edge-fn-key"]["depth"] == 2
    assert by_name["edge-fn-key"]["via"] == "supabase-jwt-secret"
    assert by_name["worker-supabase-key"]["direction"] == "related-by"
    s = result["summary"]
    assert s["other_references"] == 3
    assert s["projects"] == ["edge-fns", "project-y", "worker"]
    assert "project-y-api:docker-compose.prod.yml" in s["files"]
    assert "edge-fns:.env.production" in s["files"]
    assert "worker-supabase-key prod=file:/run/secrets/supabase" in s["injection_targets"]
    assert result["unresolved"] == [{"from": "supabase-jwt-secret", "name": "gone-ref"}]


def test_impact_max_depth_limits_hops(home):
    result = impact(_seed(), "supabase-service-key", max_depth=1)
    names = {n["name"] for n in result["nodes"]}
    assert "edge-fn-key" not in names
    assert names == {"supabase-service-key", "supabase-jwt-secret", "worker-supabase-key"}


def test_impact_is_cycle_safe(home):
    reg = Registry()
    reg.add("a", "sm-a", related=["b"])
    reg.add("b", "sm-b", related=["a"])
    result = impact(Registry(), "a")
    assert [n["name"] for n in result["nodes"]] == ["a", "b"]
    assert result["summary"]["other_references"] == 1


def test_impact_unknown_reference_raises(home):
    with pytest.raises(KeyError):
        impact(Registry(), "nope")


def test_suggestions_never_include_linked_refs_and_never_write(home):
    reg = _seed()
    before = {r.name: list(r.related) for r in reg}
    sugg = suggest_related(reg, "supabase-service-key")
    names = [s["name"] for s in sugg]
    assert names == ["supabase-anon-key"]
    reasons = sugg[0]["reasons"]
    assert any("shared source file" in r for r in reasons)
    assert any("shared group parent: project-y/supabase" in r for r in reasons)
    assert any("same repo" in r for r in reasons)
    assert {r.name: list(r.related) for r in Registry()} == before


def test_one_segment_group_parent_is_not_a_signal(home):
    reg = Registry()
    reg.add("a", "sm-a", group="project-y/x")
    reg.add("b", "sm-b", group="project-y/z")
    assert suggest_related(Registry(), "a") == []


def test_cli_impact_text(home, capsys):
    _seed()
    rc = main(["impact", "supabase-service-key"])
    out = capsys.readouterr().out
    assert rc == 0
    assert "touches 3 other references in 3 projects" in out
    assert "lists supabase-service-key as related" in out
    assert "file project-y-api:docker-compose.prod.yml" in out
    assert "gone-ref (from supabase-jwt-secret)" in out
    assert "suggested related -- NOT linked" in out
    assert "unrelated" not in out


def test_cli_impact_json_and_no_suggest(home, capsys):
    _seed()
    rc = main(["impact", "supabase-service-key", "--json", "--no-suggest"])
    data = json.loads(capsys.readouterr().out)
    assert rc == 0
    assert data["root"] == "supabase-service-key"
    assert data["suggested_related"] == []
    assert data["summary"]["other_references"] == 3


def test_cli_impact_unknown_ref(home, capsys):
    assert main(["impact", "nope"]) == 1
    assert "no reference 'nope'" in capsys.readouterr().err


def test_cli_impact_rejects_zero_depth(home):
    _seed()
    assert main(["impact", "supabase-service-key", "--depth", "0"]) == 1


def test_mcp_impact_matches_cli_json(home, capsys):
    from portunus import mcp_server
    _seed()
    main(["impact", "supabase-service-key", "--json"])
    cli = json.loads(capsys.readouterr().out)
    assert mcp_server.portunus_impact("supabase-service-key") == cli
    assert "error" in mcp_server.portunus_impact("nope")
    tool_names = {t.name for t in mcp_server.mcp._tool_manager.list_tools()}
    assert "portunus_impact" in tool_names


# --- rotation run's pre-execution preview ---------------------------------

class _FakeAdapter:
    def __init__(self):
        self.calls = 0

    def capability(self):
        return "auto"

    def rotate(self, ref, resolver=None, retire_old=False):
        from portunus.rotation import RotationResult
        self.calls += 1
        return RotationResult(ref_name=ref.name, provider="fake", phase="stored", retired_old=False)


@pytest.fixture
def fake_adapter(monkeypatch):
    from portunus import rotation as rot_mod
    adapter = _FakeAdapter()
    monkeypatch.setattr(rot_mod, "rotation_adapter_for", lambda provider: adapter)
    return adapter


def test_rotation_run_refuses_noninteractive_when_others_touched(home, capsys, fake_adapter, monkeypatch):
    reg = Registry()
    reg.add("root", "sm-a", provider="fake", project="p1", related=["dep"])
    reg.add("dep", "sm-b", project="p2")
    monkeypatch.setattr("sys.stdin.isatty", lambda: False)
    rc = main(["rotation", "run", "root"])
    err = capsys.readouterr().err
    assert rc == 1
    assert fake_adapter.calls == 0
    assert "touches 1 other reference in 2 projects" in err
    assert "--yes" in err


def test_rotation_run_yes_skips_prompt(home, capsys, fake_adapter):
    reg = Registry()
    reg.add("root", "sm-a", provider="fake", project="p1", related=["dep"])
    reg.add("dep", "sm-b", project="p2")
    rc = main(["rotation", "run", "root", "--yes"])
    captured = capsys.readouterr()
    assert rc == 0
    assert fake_adapter.calls == 1
    assert "rotated root" in captured.out
    assert "touches 1 other reference" in captured.err


def test_rotation_run_interactive_decline_cancels(home, capsys, fake_adapter, monkeypatch):
    reg = Registry()
    reg.add("root", "sm-a", provider="fake", related=["dep"])
    reg.add("dep", "sm-b")
    monkeypatch.setattr("sys.stdin.isatty", lambda: True)
    monkeypatch.setattr("builtins.input", lambda prompt="": "n")
    assert main(["rotation", "run", "root"]) == 1
    assert fake_adapter.calls == 0


def test_rotation_run_isolated_ref_never_prompts(home, capsys, fake_adapter, monkeypatch):
    Registry().add("solo", "sm-a", provider="fake", project="p1")
    monkeypatch.setattr("sys.stdin.isatty", lambda: False)
    rc = main(["rotation", "run", "solo"])
    captured = capsys.readouterr()
    assert rc == 0
    assert fake_adapter.calls == 1
    assert "touches 0 other references in 1 project" in captured.err
