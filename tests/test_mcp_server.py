"""portunus mcp -- FastMCP stdio server (story 01, portunus-mcp-server).

portunus_health is deliberately trivial (mirrors ui/app/api/health/route.ts):
a pure liveness signal, never touches the registry/backend."""
import ast
import inspect
import textwrap


def test_mcp_module_exposes_a_fastmcp_instance():
    from portunus import mcp_server
    assert mcp_server.mcp.name == "portunus"


def test_portunus_health_registered_as_a_tool():
    from portunus import mcp_server
    tool_names = {t.name for t in mcp_server.mcp._tool_manager.list_tools()}
    assert "portunus_health" in tool_names


def test_portunus_health_returns_ok_without_touching_registry():
    from portunus import mcp_server
    result = mcp_server.portunus_health()
    assert "ok" in result.lower()


def test_portunus_health_source_never_imports_registry():
    """Structural: portunus_health must be as trivial as the UI's own
    /api/health -- no Registry/Resolver/Backend import, no process-env
    dependency on PORTUNUS_HOME."""
    from portunus import mcp_server
    src = textwrap.dedent(inspect.getsource(mcp_server.portunus_health))
    tree = ast.parse(src)
    func = tree.body[0]
    body = func.body[1:] if ast.get_docstring(func) else func.body
    code_src = "\n".join(ast.unparse(node) for node in body)
    assert "Registry" not in code_src
    assert "Resolver" not in code_src
    assert ".access(" not in code_src


def test_cli_mcp_subcommand_registered():
    from portunus.cli import build_parser
    parser = build_parser()
    # argparse doesn't expose subparser names directly; check via parse
    # that "mcp" is a recognized choice by inspecting the subparsers action.
    sub_actions = [a for a in parser._actions if a.dest == "cmd"]
    assert sub_actions
    assert "mcp" in sub_actions[0].choices


# --- story 02: read-only metadata tools -----------------------------------

def _no_backend_access(fn):
    src = textwrap.dedent(inspect.getsource(fn))
    tree = ast.parse(src)
    func = tree.body[0]
    body = func.body[1:] if ast.get_docstring(func) else func.body
    return "\n".join(ast.unparse(node) for node in body)


def test_portunus_list_returns_metadata(home):
    from portunus import Registry
    from portunus import mcp_server
    reg = Registry()
    reg.add("x", "sm-x", project="demo", description="a key")
    result = mcp_server.portunus_list("demo")
    assert len(result) == 1
    assert result[0]["name"] == "x"
    assert result[0]["description"] == "a key"


def test_portunus_list_no_backend_access():
    from portunus import mcp_server
    code = _no_backend_access(mcp_server.portunus_list)
    assert ".access(" not in code


def test_portunus_tree_matches_cli_json_shape(home):
    from portunus import Registry
    from portunus import mcp_server
    reg = Registry()
    reg.add("x", "sm-x", project="demo", group="a/b")
    reg.add("y", "sm-y", project="demo")
    result = mcp_server.portunus_tree("demo")
    assert "x" in result["tree"]["a"]["b"]["_refs"]
    assert "y" in result["ungrouped"]
    assert "refs" in result


def test_portunus_tree_no_backend_access():
    from portunus import mcp_server
    code = _no_backend_access(mcp_server.portunus_tree)
    assert ".access(" not in code


def test_portunus_ask_preview_fetch_resolves(home):
    from portunus import Registry
    from portunus import mcp_server
    reg = Registry()
    reg.add("a", "sm-a", provider="vercel", project="mdostal.com", env="prod")
    result = mcp_server.portunus_ask_preview("the vercel secret for mdostal.com in prod")
    assert result["name"] == "a"


def test_portunus_ask_preview_rejects_non_fetch_intent(home):
    from portunus import Registry
    from portunus import mcp_server
    reg = Registry()
    reg.add("a", "sm-a", provider="vercel", project="mdostal.com")
    result = mcp_server.portunus_ask_preview("rotate the vercel secret for mdostal.com")
    assert "error" in result
    assert "not a fetch request" in result["error"]


def test_portunus_ask_preview_ambiguous_reports_candidates(home):
    from portunus import Registry
    from portunus import mcp_server
    reg = Registry()
    reg.add("a", "sm-a", provider="vercel", project="mdostal.com", env="prod")
    reg.add("b", "sm-b", provider="vercel", project="mdostal.com", env="staging")
    result = mcp_server.portunus_ask_preview("the vercel secret for mdostal.com")
    assert "error" in result


def test_portunus_ask_preview_no_backend_access():
    from portunus import mcp_server
    code = _no_backend_access(mcp_server.portunus_ask_preview)
    assert ".access(" not in code


def test_portunus_bindings_show_returns_configured_bindings(home):
    from portunus.backend import GcpProjectBinding, save_gcp_bindings
    from portunus import mcp_server
    save_gcp_bindings({"demo": GcpProjectBinding("demo", account="user@example.com")})
    result = mcp_server.portunus_bindings_show()
    assert result["demo"]["account"] == "user@example.com"


def test_portunus_bindings_show_single_project(home):
    from portunus.backend import GcpProjectBinding, save_gcp_bindings
    from portunus import mcp_server
    save_gcp_bindings({
        "a": GcpProjectBinding("a", account="a@example.com"),
        "b": GcpProjectBinding("b", account="b@example.com"),
    })
    result = mcp_server.portunus_bindings_show("a")
    assert list(result.keys()) == ["a"]
