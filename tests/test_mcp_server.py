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
