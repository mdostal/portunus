"""`portunus agent init`/`status` -- CLI surface over agent_setup.py. This
feature has zero business touching the vault at all (it's local agent-CLI
config plumbing), so the structural test below proves that rather than just
assuming it from the description."""
import ast
import inspect
import json

from portunus import agent_setup
from portunus.cli import cmd_agent_init, cmd_agent_status, main


def test_agent_init_text_output(monkeypatch, capsys):
    monkeypatch.setattr(
        agent_setup, "agent_init",
        lambda only=None, dest=None: {
            "harnesses": {"claude": True, "codex": False},
            "requested": ["claude"],
            "mcp_registered": {"claude": True},
            "skills_installed": ["portunus-ask"],
        },
    )
    rc = main(["agent", "init"])
    out = capsys.readouterr().out
    assert rc == 0
    assert "claude: MCP server registered" in out
    assert "codex: not found on this machine, skipped" in out
    assert "portunus-ask" in out


def test_agent_init_json_flag(monkeypatch, capsys):
    payload = {
        "harnesses": {"claude": True, "codex": False},
        "requested": ["claude"],
        "mcp_registered": {"claude": True},
        "skills_installed": [],
    }
    monkeypatch.setattr(agent_setup, "agent_init", lambda only=None, dest=None: payload)
    rc = main(["agent", "init", "--json"])
    out = capsys.readouterr().out
    assert rc == 0
    assert json.loads(out) == payload


def test_agent_init_harness_flag_is_passed_through(monkeypatch):
    seen = {}

    def fake_init(only=None, dest=None):
        seen["only"] = only
        return {"harnesses": {}, "requested": only or [], "mcp_registered": {}, "skills_installed": []}

    monkeypatch.setattr(agent_setup, "agent_init", fake_init)
    main(["agent", "init", "--harness", "codex"])
    assert seen["only"] == ["codex"]


def test_agent_init_reports_failed_registration(monkeypatch, capsys):
    monkeypatch.setattr(
        agent_setup, "agent_init",
        lambda only=None, dest=None: {
            "harnesses": {"claude": True, "codex": False},
            "requested": ["claude"],
            "mcp_registered": {"claude": False},
            "skills_installed": [],
        },
    )
    rc = main(["agent", "init"])
    out = capsys.readouterr().out
    assert rc == 0
    assert "claude: MCP server FAILED to register" in out


def test_agent_status_text_output(monkeypatch, capsys):
    monkeypatch.setattr(
        agent_setup, "agent_status",
        lambda dest=None: {
            "harnesses": {"claude": True, "codex": True},
            "mcp_registered": {"claude": True, "codex": False},
            "skills": {"portunus-ask": True, "portunus-drop": False},
        },
    )
    rc = main(["agent", "status"])
    out = capsys.readouterr().out
    assert rc == 0
    assert "claude: present, MCP server registered" in out
    assert "codex: present, MCP server NOT registered" in out
    assert "skills installed: portunus-ask" in out
    assert "skills missing: portunus-drop" in out


def test_agent_status_json_flag(monkeypatch, capsys):
    payload = {
        "harnesses": {"claude": False, "codex": False},
        "mcp_registered": {"claude": False, "codex": False},
        "skills": {},
    }
    monkeypatch.setattr(agent_setup, "agent_status", lambda dest=None: payload)
    rc = main(["agent", "status", "--json"])
    out = capsys.readouterr().out
    assert rc == 0
    assert json.loads(out) == payload


def test_agent_status_never_mutates(tmp_path, monkeypatch):
    """`status` must never call agent_init/register_mcp/install_skills --
    it's read-only by contract."""
    monkeypatch.setattr(agent_setup, "register_mcp", lambda h: (_ for _ in ()).throw(AssertionError("mutated")))
    monkeypatch.setattr(agent_setup, "install_skills", lambda dest=None: (_ for _ in ()).throw(AssertionError("mutated")))
    main(["agent", "status"])  # would raise via the poisoned functions above if status ever called them


def test_cmd_agent_functions_never_touch_the_secret_boundary():
    """Structural guard: this feature is local agent-CLI config plumbing and
    has zero legitimate reason to import or reference the vault machinery."""
    forbidden = ("Registry", "Broker", "Resolver", "backend", "resolver", "value")
    for fn in (cmd_agent_init, cmd_agent_status):
        src = inspect.getsource(fn)
        tree = ast.parse(src)
        code = ast.unparse(tree)
        for term in forbidden:
            assert term not in code, f"{fn.__name__} references {term!r} -- this command should never touch the vault"


def test_agent_setup_module_never_touches_the_secret_boundary():
    """AST-based, not a raw substring check -- the module's own docstring
    names Registry/Broker/Resolver explicitly (to say it never imports
    them), which would false-positive a naive text search."""
    tree = ast.parse(inspect.getsource(agent_setup))
    imported_names = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            imported_names.update(alias.name for alias in node.names)
        elif isinstance(node, ast.ImportFrom):
            imported_names.update(alias.name for alias in node.names)
    forbidden = {"Registry", "Broker", "Resolver", "SecretBackend"}
    assert not (imported_names & forbidden), f"agent_setup.py imports {imported_names & forbidden} -- should never touch the vault"
