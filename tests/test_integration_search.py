"""Integration tests for the search feature — all three entry points.

CLI:  portunus search <query> [--json] [--project ...] [--state ...]
MCP:  portunus_search(query, ...) via mcp_server
UI:   subprocess invocation shape that /api/search uses (same pattern as
      test_ui_gate.py — the route is a thin wrapper; testing the subprocess
      IS the route test without running a Next.js server)
"""
import json
import os
import subprocess

import pytest

from portunus import Registry
from portunus.cli import main


# ---------------------------------------------------------------------------
# Shared helpers
# ---------------------------------------------------------------------------

def _populate(reg: Registry) -> None:
    reg.add("linear-token", "sm-linear-token", project="ffe-cicd",
            description="Linear personal API key", purpose="issue tracker",
            provider="gcp", env="prod", state="enabled",
            tags={"service": "linear"})
    reg.add("discord-bot-token", "sm-discord-bot", project="ffe-cicd",
            description="Discord bot OAuth token", purpose="notifications",
            provider="gcp", env="prod", state="enabled")
    reg.add("stripe-secret-key", "sm-stripe", project="mdostal.com",
            description="Stripe live key", purpose="billing",
            provider="gcp", env="prod", state="enabled")
    reg.add("old-key", "sm-old-key", project="ffe-cicd",
            description="decommissioned legacy key", purpose="",
            provider="gcp", env="prod", state="dropped")


def _run_subprocess(args, home, *, env_extra=None):
    env = dict(os.environ)
    env["PORTUNUS_HOME"] = str(home)
    env["USER"] = "tester"
    env.pop("DOSTAL_AGENT", None)
    env.pop("DOSTAL_TASK", None)
    if env_extra:
        env.update(env_extra)
    return subprocess.run(
        ["portunus", *args], env=env,
        capture_output=True, text=True, timeout=10,
    )


# ===========================================================================
# CLI entry point
# ===========================================================================

def test_cli_search_registered_as_subcommand():
    from portunus.cli import build_parser
    parser = build_parser()
    sub_actions = [a for a in parser._actions if a.dest == "cmd"]
    assert sub_actions
    assert "search" in sub_actions[0].choices


def test_cli_search_basic_match(home, capsys):
    reg = Registry()
    _populate(reg)
    rc = main(["search", "linear"])
    out = capsys.readouterr().out
    assert rc == 0
    assert "linear-token" in out


def test_cli_search_no_matches(home, capsys):
    reg = Registry()
    _populate(reg)
    rc = main(["search", "notarealterm12345"])
    out = capsys.readouterr().out
    assert rc == 0
    assert "no matches" in out


def test_cli_search_json_output(home, capsys):
    reg = Registry()
    _populate(reg)
    rc = main(["search", "linear", "--json"])
    out = capsys.readouterr().out
    assert rc == 0
    data = json.loads(out)
    assert isinstance(data, list)
    assert any(r["name"] == "linear-token" for r in data)
    # never a value in any result
    for ref in data:
        assert "value" not in ref


def test_cli_search_json_empty_result(home, capsys):
    reg = Registry()
    _populate(reg)
    rc = main(["search", "notarealterm12345", "--json"])
    out = capsys.readouterr().out
    assert rc == 0
    assert json.loads(out) == []


def test_cli_search_project_filter(home, capsys):
    reg = Registry()
    _populate(reg)
    # "stripe" only in mdostal.com; searching in ffe-cicd should return nothing
    rc = main(["search", "stripe", "--project", "ffe-cicd", "--json"])
    out = capsys.readouterr().out
    assert rc == 0
    assert json.loads(out) == []

    # searching in mdostal.com should find it
    rc2 = main(["search", "stripe", "--project", "mdostal.com", "--json"])
    out2 = capsys.readouterr().out
    assert rc2 == 0
    data = json.loads(out2)
    assert len(data) == 1
    assert data[0]["name"] == "stripe-secret-key"


def test_cli_search_state_filter(home, capsys):
    reg = Registry()
    _populate(reg)
    rc = main(["search", "key", "--state", "enabled", "--json"])
    out = capsys.readouterr().out
    assert rc == 0
    data = json.loads(out)
    # old-key (dropped) must not appear
    assert all(r["state"] == "enabled" for r in data)
    assert not any(r["name"] == "old-key" for r in data)


def test_cli_search_case_insensitive(home, capsys):
    reg = Registry()
    _populate(reg)
    rc = main(["search", "LINEAR", "--json"])
    out = capsys.readouterr().out
    assert rc == 0
    data = json.loads(out)
    assert any(r["name"] == "linear-token" for r in data)


def test_cli_search_enabled_sorted_first(home, capsys):
    reg = Registry()
    _populate(reg)
    # "key" matches both linear-token and old-key (dropped)
    rc = main(["search", "key", "--json"])
    out = capsys.readouterr().out
    assert rc == 0
    data = json.loads(out)
    names = [r["name"] for r in data]
    # old-key is dropped; enabled refs appear before it
    assert names.index("old-key") > 0 or len(names) == 1


# ===========================================================================
# MCP entry point
# ===========================================================================

def test_mcp_portunus_search_registered_as_tool():
    from portunus import mcp_server
    tool_names = {t.name for t in mcp_server.mcp._tool_manager.list_tools()}
    assert "portunus_search" in tool_names


def test_mcp_portunus_search_returns_matching_refs(home):
    from portunus import mcp_server
    reg = Registry()
    _populate(reg)
    result = mcp_server.portunus_search("linear")
    assert isinstance(result, list)
    assert any(r["name"] == "linear-token" for r in result)


def test_mcp_portunus_search_empty_query_returns_empty(home):
    from portunus import mcp_server
    reg = Registry()
    _populate(reg)
    result = mcp_server.portunus_search("")
    assert result == []


def test_mcp_portunus_search_project_filter(home):
    from portunus import mcp_server
    reg = Registry()
    _populate(reg)
    result = mcp_server.portunus_search("stripe", project="ffe-cicd")
    assert result == []
    result2 = mcp_server.portunus_search("stripe", project="mdostal.com")
    assert len(result2) == 1
    assert result2[0]["name"] == "stripe-secret-key"


def test_mcp_portunus_search_no_backend_access():
    """Structural: portunus_search must never call .access() -- metadata only."""
    import ast
    import inspect
    import textwrap
    from portunus import mcp_server
    src = textwrap.dedent(inspect.getsource(mcp_server.portunus_search))
    assert ".access(" not in src


def test_mcp_portunus_search_result_contains_no_value(home):
    from portunus import mcp_server
    reg = Registry()
    _populate(reg)
    result = mcp_server.portunus_search("linear")
    for ref in result:
        assert "value" not in ref


# ===========================================================================
# UI subprocess shape (what /api/search/route.ts shells out to)
# ===========================================================================

def test_ui_search_subprocess_basic(home):
    reg = Registry()
    _populate(reg)
    proc = _run_subprocess(["search", "linear", "--json"], home)
    assert proc.returncode == 0
    data = json.loads(proc.stdout)
    assert any(r["name"] == "linear-token" for r in data)


def test_ui_search_subprocess_empty_result(home):
    reg = Registry()
    _populate(reg)
    proc = _run_subprocess(["search", "notarealterm12345", "--json"], home)
    assert proc.returncode == 0
    assert json.loads(proc.stdout) == []


def test_ui_search_subprocess_project_filter(home):
    reg = Registry()
    _populate(reg)
    proc = _run_subprocess(
        ["search", "stripe", "--project", "ffe-cicd", "--json"], home
    )
    assert proc.returncode == 0
    assert json.loads(proc.stdout) == []


def test_ui_search_subprocess_never_leaks_value(home):
    reg = Registry()
    _populate(reg)
    proc = _run_subprocess(["search", "linear", "--json"], home)
    assert proc.returncode == 0
    data = json.loads(proc.stdout)
    for ref in data:
        assert "value" not in ref
    # also check stdout/stderr do not contain any raw secret-value-shaped string
    assert "s3kr3t" not in proc.stdout
    assert "s3kr3t" not in proc.stderr
