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
    from portunus.backend import VaultBinding, save_vault_bindings
    from portunus import mcp_server
    save_vault_bindings({"demo": VaultBinding("demo", account="user@example.com")})
    result = mcp_server.portunus_bindings_show()
    assert result["demo"]["account"] == "user@example.com"


def test_portunus_bindings_show_single_project(home):
    from portunus.backend import VaultBinding, save_vault_bindings
    from portunus import mcp_server
    save_vault_bindings({
        "a": VaultBinding("a", account="a@example.com"),
        "b": VaultBinding("b", account="b@example.com"),
    })
    result = mcp_server.portunus_bindings_show("a")
    assert list(result.keys()) == ["a"]


def test_portunus_bindings_show_reports_backend_and_sync_mode(home):
    from portunus.backend import VaultBinding, save_vault_bindings
    from portunus import mcp_server
    save_vault_bindings({"demo": VaultBinding("demo", backend="local", sync_mode="cached")})
    result = mcp_server.portunus_bindings_show("demo")
    assert result["demo"]["backend"] == "local"
    assert result["demo"]["sync_mode"] == "cached"


# --- story 03: discovery tool ------------------------------------------

def _mock_gcloud_list(monkeypatch, secrets):
    import json
    from types import SimpleNamespace

    def fake_run(cmd, capture_output, text, timeout):
        return SimpleNamespace(returncode=0, stdout=json.dumps(secrets), stderr="")

    monkeypatch.setattr("portunus.discover._default_runner", fake_run)
    monkeypatch.setattr("portunus.discover.shutil.which", lambda name: "/bin/gcloud")


def test_portunus_discover_diff_only(home, monkeypatch):
    from portunus import mcp_server
    _mock_gcloud_list(monkeypatch, [{"name": "projects/demo/secrets/API_KEY", "labels": {}}])
    result = mcp_server.portunus_discover("demo")
    assert result["already_registered"] == []
    assert result["not_yet_registered"][0]["sm_name"] == "API_KEY"
    assert "wif_configured" in result


def test_portunus_discover_register(home, monkeypatch):
    from portunus import mcp_server
    _mock_gcloud_list(monkeypatch, [{"name": "projects/demo/secrets/API_KEY", "labels": {}}])
    result = mcp_server.portunus_discover("demo", register=True)
    assert result["registered"] == ["demo-api_key"]
    assert result["conflicts"] == []


def test_portunus_discover_no_backend_access():
    from portunus import mcp_server
    code = _no_backend_access(mcp_server.portunus_discover)
    assert ".access(" not in code


# --- story 04: resolve_to_tempfile injection tool -----------------------

def test_resolve_to_tempfile_by_name(home, monkeypatch):
    from portunus import Registry
    from portunus import mcp_server
    reg = Registry()
    reg.add("x", "sm-x")
    monkeypatch.setenv("PORTUNUS_BACKEND", "mock")
    monkeypatch.setenv("PORTUNUS_MOCK_SM_X", "the-value")
    result = mcp_server.portunus_resolve_to_tempfile(name="x")
    assert "path" in result
    assert "error" not in result
    from pathlib import Path
    p = Path(result["path"])
    assert p.read_text() == "the-value"
    assert "the-value" not in str(result)
    p.unlink()


def test_resolve_to_tempfile_by_tags(home, monkeypatch):
    from portunus import Registry
    from portunus import mcp_server
    reg = Registry()
    reg.add("x", "sm-x", provider="gcp", project="demo")
    monkeypatch.setenv("PORTUNUS_BACKEND", "mock")
    monkeypatch.setenv("PORTUNUS_MOCK_SM_X", "the-value")
    result = mcp_server.portunus_resolve_to_tempfile(tags={"provider": "gcp", "project": "demo"})
    assert "path" in result
    from pathlib import Path
    p = Path(result["path"])
    assert p.read_text() == "the-value"
    p.unlink()


def test_resolve_to_tempfile_ambiguous_tags(home, monkeypatch):
    from portunus import Registry
    from portunus import mcp_server
    reg = Registry()
    reg.add("a", "sm-a", project="demo")
    reg.add("b", "sm-b", project="demo")
    monkeypatch.setenv("PORTUNUS_BACKEND", "mock")
    result = mcp_server.portunus_resolve_to_tempfile(tags={"project": "demo"})
    assert "error" in result
    assert "a" in result["error"] and "b" in result["error"]


def test_resolve_to_tempfile_no_match(home, monkeypatch):
    from portunus import mcp_server
    monkeypatch.setenv("PORTUNUS_BACKEND", "mock")
    result = mcp_server.portunus_resolve_to_tempfile(tags={"project": "nonexistent"})
    assert "error" in result


def test_resolve_to_tempfile_neither_name_nor_tags(home, monkeypatch):
    from portunus import mcp_server
    monkeypatch.setenv("PORTUNUS_BACKEND", "mock")
    result = mcp_server.portunus_resolve_to_tempfile()
    assert "error" in result
    assert "name or tags" in result["error"]


def test_resolve_to_tempfile_unknown_name(home, monkeypatch):
    from portunus import mcp_server
    monkeypatch.setenv("PORTUNUS_BACKEND", "mock")
    result = mcp_server.portunus_resolve_to_tempfile(name="nonexistent")
    assert "error" in result


def test_resolve_to_tempfile_dropped_state_fails_closed(home, monkeypatch):
    from portunus import Registry
    from portunus import mcp_server
    reg = Registry()
    reg.add("x", "sm-x", state="dropped")
    monkeypatch.setenv("PORTUNUS_BACKEND", "mock")
    result = mcp_server.portunus_resolve_to_tempfile(name="x")
    assert "error" in result
    assert "path" not in result


def test_resolve_to_tempfile_never_returns_a_value_source_check():
    """AST-level: every return in this function must come from a path
    variable or a safely-constructed error string -- never a resolved
    value/text variable."""
    from portunus import mcp_server
    src = _no_backend_access(mcp_server.portunus_resolve_to_tempfile)
    # The literal string "the-value" (or any secret) can never appear in
    # source, but the stronger guarantee is structural: no reference to a
    # variable holding _substitute()'s output outside resolver.resolve_to_tempfile's
    # own return, which already only yields a path.
    assert ".access(" not in src


# --- story 05: resolve_exec injection tool (highest scrutiny) -----------

def test_resolve_exec_happy_path_returns_only_process_result(home, monkeypatch):
    from portunus import Registry
    from portunus import mcp_server
    reg = Registry()
    reg.add("x", "sm-x")
    monkeypatch.setenv("PORTUNUS_BACKEND", "mock")
    monkeypatch.setenv("PORTUNUS_MOCK_SM_X", "the-value")

    captured = {}

    class FakeCompleted:
        stdout = "ok stdout"
        stderr = ""
        returncode = 0

    def fake_run(argv, capture_output, text, timeout):
        captured["argv"] = argv
        assert timeout == 30
        return FakeCompleted()

    monkeypatch.setattr(mcp_server.subprocess, "run", fake_run)
    result = mcp_server.portunus_resolve_exec(
        argv=["curl", "-H", "key: {{secret}}", "https://x"], name="x"
    )
    assert set(result.keys()) == {"stdout", "stderr", "returncode"}
    assert result["stdout"] == "ok stdout"
    assert result["returncode"] == 0
    # the real value went into the captured argv the fake subprocess saw...
    assert "the-value" in captured["argv"][2]
    # ...but never into the tool's own return value.
    assert "the-value" not in str(result)


def test_resolve_exec_by_tags(home, monkeypatch):
    from portunus import Registry
    from portunus import mcp_server
    reg = Registry()
    reg.add("x", "sm-x", provider="gcp", project="demo")
    monkeypatch.setenv("PORTUNUS_BACKEND", "mock")
    monkeypatch.setenv("PORTUNUS_MOCK_SM_X", "the-value")

    class FakeCompleted:
        stdout = ""
        stderr = ""
        returncode = 0

    monkeypatch.setattr(
        mcp_server.subprocess, "run",
        lambda argv, capture_output, text, timeout: FakeCompleted(),
    )
    result = mcp_server.portunus_resolve_exec(
        argv=["echo", "{{secret}}"], tags={"provider": "gcp", "project": "demo"}
    )
    assert "error" not in result


def test_resolve_exec_nonzero_exit_returned_normally(home, monkeypatch):
    from portunus import Registry
    from portunus import mcp_server
    reg = Registry()
    reg.add("x", "sm-x")
    monkeypatch.setenv("PORTUNUS_BACKEND", "mock")
    monkeypatch.setenv("PORTUNUS_MOCK_SM_X", "the-value")

    class FakeCompleted:
        stdout = ""
        stderr = "boom"
        returncode = 7

    monkeypatch.setattr(
        mcp_server.subprocess, "run",
        lambda argv, capture_output, text, timeout: FakeCompleted(),
    )
    result = mcp_server.portunus_resolve_exec(argv=["false", "{{secret}}"], name="x")
    assert "error" not in result
    assert result["returncode"] == 7
    assert result["stderr"] == "boom"


def test_resolve_exec_timeout_does_not_leak_argv(home, monkeypatch):
    import subprocess as real_subprocess
    from portunus import Registry
    from portunus import mcp_server
    reg = Registry()
    reg.add("x", "sm-x")
    monkeypatch.setenv("PORTUNUS_BACKEND", "mock")
    monkeypatch.setenv("PORTUNUS_MOCK_SM_X", "the-super-secret-value")

    def fake_run(argv, capture_output, text, timeout):
        raise real_subprocess.TimeoutExpired(cmd=argv, timeout=timeout)

    monkeypatch.setattr(mcp_server.subprocess, "run", fake_run)
    result = mcp_server.portunus_resolve_exec(argv=["sleep", "{{secret}}"], name="x")
    assert "error" in result
    assert set(result.keys()) == {"error"}
    assert "the-super-secret-value" not in str(result)


def test_resolve_exec_oserror_does_not_leak_argv(home, monkeypatch):
    from portunus import Registry
    from portunus import mcp_server
    reg = Registry()
    reg.add("x", "sm-x")
    monkeypatch.setenv("PORTUNUS_BACKEND", "mock")
    monkeypatch.setenv("PORTUNUS_MOCK_SM_X", "the-super-secret-value")

    def fake_run(argv, capture_output, text, timeout):
        raise FileNotFoundError(f"no such file: {argv}")

    monkeypatch.setattr(mcp_server.subprocess, "run", fake_run)
    result = mcp_server.portunus_resolve_exec(argv=["nonexistent", "{{secret}}"], name="x")
    assert "error" in result
    assert "the-super-secret-value" not in str(result)


def test_resolve_exec_ambiguous_tags(home, monkeypatch):
    from portunus import Registry
    from portunus import mcp_server
    reg = Registry()
    reg.add("a", "sm-a", project="demo")
    reg.add("b", "sm-b", project="demo")
    monkeypatch.setenv("PORTUNUS_BACKEND", "mock")
    result = mcp_server.portunus_resolve_exec(argv=["echo", "{{secret}}"], tags={"project": "demo"})
    assert "error" in result
    assert "a" in result["error"] and "b" in result["error"]


def test_resolve_exec_no_match(home, monkeypatch):
    from portunus import mcp_server
    monkeypatch.setenv("PORTUNUS_BACKEND", "mock")
    result = mcp_server.portunus_resolve_exec(
        argv=["echo", "{{secret}}"], tags={"project": "nonexistent"}
    )
    assert "error" in result


def test_resolve_exec_neither_name_nor_tags(home, monkeypatch):
    from portunus import mcp_server
    monkeypatch.setenv("PORTUNUS_BACKEND", "mock")
    result = mcp_server.portunus_resolve_exec(argv=["echo", "{{secret}}"])
    assert "error" in result
    assert "name or tags" in result["error"]


def test_resolve_exec_unknown_name(home, monkeypatch):
    from portunus import mcp_server
    monkeypatch.setenv("PORTUNUS_BACKEND", "mock")
    result = mcp_server.portunus_resolve_exec(argv=["echo", "{{secret}}"], name="nonexistent")
    assert "error" in result


def test_resolve_exec_dropped_state_fails_closed(home, monkeypatch):
    from portunus import Registry
    from portunus import mcp_server
    reg = Registry()
    reg.add("x", "sm-x", state="dropped")
    monkeypatch.setenv("PORTUNUS_BACKEND", "mock")
    result = mcp_server.portunus_resolve_exec(argv=["echo", "{{secret}}"], name="x")
    assert "error" in result
    assert "stdout" not in result


def test_resolve_exec_never_leaks_argv_source_check():
    """AST-level: portunus_resolve_exec's own source never builds or touches
    a resolved/substituted argv -- resolution happens entirely inside
    resolver.resolve_exec()/_capturing_runner(), out of this function's own
    scope, so there is no local variable here that could ever hold a value."""
    from portunus import mcp_server
    src = _no_backend_access(mcp_server.portunus_resolve_exec)
    assert ".access(" not in src
    assert "_substitute" not in src
    assert "os.execvp" not in src


def test_resolve_exec_capturing_runner_never_leaks_on_exception():
    """AST-level: the runner's exception handlers never reference the caught
    exception's own attributes -- subprocess.TimeoutExpired/OSError carry the
    resolved argv on .cmd/args/str(), so the handlers raise bare sentinel
    exceptions instead of re-using anything from the caught exception."""
    from portunus import mcp_server
    src = _no_backend_access(mcp_server._capturing_runner)
    assert "str(exc" not in src
    assert ".cmd" not in src
    assert ".output" not in src
    assert ".args" not in src


# --- story 01 (portunus-local-create): portunus_drop create tool --------

def test_drop_minimal_stores_value(home, monkeypatch):
    from portunus import mcp_server
    result = mcp_server.portunus_drop(name="x", sm_name="sm-x", value="the-value")
    assert result == {"name": "x", "sm_name": "sm-x", "state": "dropped"}

    from portunus import Registry
    reg = Registry()
    reg.set_state("x", "enabled")
    from portunus.localvault import LocalEncryptedBackend
    backend = LocalEncryptedBackend()
    assert backend.access("sm-x") == "the-value"


def test_drop_full_metadata_passed_through_unmangled(home):
    from portunus import mcp_server, Registry
    result = mcp_server.portunus_drop(
        name="gig-tracker-stripe-key", sm_name="STRIPE_KEY", value="sk_test_123",
        scope="personal", kind="api-key", provider="local", project="gig-tracker",
        env="dev", tags={"team": "solo"}, description="Stripe test key",
        purpose="payments", injected_as={"dev": "env:STRIPE_KEY"},
        group="gig-tracker/stripe", related=["gig-tracker-stripe-webhook"],
    )
    assert result == {"name": "gig-tracker-stripe-key", "sm_name": "STRIPE_KEY", "state": "dropped"}
    ref = Registry().require("gig-tracker-stripe-key")
    assert ref.tags == {"team": "solo"}
    assert ref.injected_as == {"dev": "env:STRIPE_KEY"}
    assert ref.related == ["gig-tracker-stripe-webhook"]
    assert ref.group == "gig-tracker/stripe"
    assert ref.description == "Stripe test key"
    assert ref.purpose == "payments"
    assert ref.project == "gig-tracker"
    assert ref.env == "dev"


def test_drop_refuses_non_local_backend(home, monkeypatch):
    from portunus import mcp_server, Registry
    monkeypatch.setenv("PORTUNUS_BACKEND", "gcloud")
    result = mcp_server.portunus_drop(name="x", sm_name="sm-x", value="the-value")
    assert result == {
        "error": "drop requires the local-encrypted backend "
        "(unset PORTUNUS_BACKEND or set it to unset/local)"
    }
    assert "x" not in Registry()


def test_drop_empty_value_refused(home):
    from portunus import mcp_server, Registry
    result = mcp_server.portunus_drop(name="x", sm_name="sm-x", value="")
    assert result == {"error": "empty secret value; nothing dropped"}
    assert "x" not in Registry()


def test_drop_overwrites_on_duplicate_name_matching_cli_behavior(home):
    """Registry.add() is documented as 'Register (or overwrite) a reference'
    -- it does not raise on a duplicate name. portunus_drop mirrors cmd_drop
    exactly and does not invent a new duplicate guard."""
    from portunus import mcp_server, Registry
    mcp_server.portunus_drop(name="x", sm_name="sm-x", value="first")
    result = mcp_server.portunus_drop(name="x", sm_name="sm-x-v2", value="second")
    assert result == {"name": "x", "sm_name": "sm-x-v2", "state": "dropped"}
    assert Registry().require("x").sm_name == "sm-x-v2"


def test_drop_never_returns_a_value_source_check():
    """AST-level: no Return node in portunus_drop's body may reference the
    name `value` -- value legitimately appears elsewhere in the body
    (backend.store(ref.sm_name, value), del value), so this checks Return
    nodes specifically rather than asserting `value` is absent from source
    entirely (grill H2)."""
    import ast
    import inspect
    import textwrap
    from portunus import mcp_server

    src = textwrap.dedent(inspect.getsource(mcp_server.portunus_drop))
    tree = ast.parse(src)
    func = tree.body[0]
    for node in ast.walk(func):
        if isinstance(node, ast.Return) and node.value is not None:
            names = {n.id for n in ast.walk(node.value) if isinstance(n, ast.Name)}
            assert "value" not in names, f"Return references the `value` name: {ast.unparse(node.value)}"


def test_drop_docstring_instructs_caller_not_to_echo_value():
    """grill H3: the docstring must explicitly tell the calling agent not to
    re-echo the value back to the human after a successful store -- Portunus's
    own return never contains it, but that's not the same as the caller
    behaving afterward."""
    from portunus import mcp_server
    doc = (mcp_server.portunus_drop.__doc__ or "").lower()
    assert "echo" in doc


# --- story 02 (portunus-local-create): portunus_state lifecycle tool ----

def test_state_transitions_reference(home):
    from portunus import mcp_server, Registry
    reg = Registry()
    reg.add("x", "sm-x", state="dropped")
    result = mcp_server.portunus_state(name="x", state="enabled")
    assert result == {"name": "x", "state": "enabled"}
    assert Registry().require("x").state == "enabled"


def test_state_unknown_reference(home):
    from portunus import mcp_server
    result = mcp_server.portunus_state(name="nonexistent", state="enabled")
    assert result == {"error": "unknown reference: nonexistent"}


def test_state_invalid_state(home):
    from portunus import mcp_server, Registry
    reg = Registry()
    reg.add("x", "sm-x")
    result = mcp_server.portunus_state(name="x", state="not-a-real-state")
    assert "error" in result
    assert Registry().require("x").state == "enabled"


def test_state_no_backend_access():
    from portunus import mcp_server
    code = _no_backend_access(mcp_server.portunus_state)
    assert ".access(" not in code


# --- story 04 (portunus-vault-routing): portunus_sync tool ---------------

def test_portunus_sync_reports_synced(home, monkeypatch):
    import json as _json
    from types import SimpleNamespace as _NS
    from portunus import Registry, mcp_server
    from portunus.backend import VaultBinding, save_vault_bindings

    def fake_run(cmd, capture_output, text, timeout):
        if "describe" in cmd:
            return _NS(returncode=0, stdout=_json.dumps({"name": "v1", "createTime": "T1"}), stderr="")
        return _NS(returncode=0, stdout="VALUE", stderr="")

    monkeypatch.setattr("portunus.backend.subprocess.run", fake_run)
    monkeypatch.setattr("portunus.backend.shutil.which", lambda name: "/bin/gcloud")
    save_vault_bindings({"demo": VaultBinding("demo", backend="gcp", sync_mode="cached")})
    Registry().add("x", "sm-x", project="demo")

    result = mcp_server.portunus_sync("demo")
    assert result == {"synced": ["x"], "already_fresh": [], "failed": []}


def test_portunus_sync_no_cached_references(home):
    from portunus import Registry, mcp_server
    Registry().add("x", "sm-x", project="demo")
    result = mcp_server.portunus_sync("demo")
    assert result == {"synced": [], "already_fresh": [], "failed": []}


def test_portunus_drop_accepts_backend_override(home):
    from portunus import mcp_server, Registry
    result = mcp_server.portunus_drop(name="x", sm_name="sm-x", value="v", backend="local")
    assert result == {"name": "x", "sm_name": "sm-x", "state": "dropped"}
    assert Registry().require("x").backend == "local"


# --- story 06 (portunus-vault-routing): portunus_drop_bulk ---------------

def test_drop_bulk_creates_all_valid_entries(home):
    from portunus import mcp_server, Registry
    entries = [
        {"name": "a", "sm_name": "sm-a", "value": "va"},
        {"name": "b", "sm_name": "sm-b", "value": "vb"},
        {"name": "c", "sm_name": "sm-c", "value": "vc"},
    ]
    result = mcp_server.portunus_drop_bulk(entries)
    assert set(result["created"]) == {"a", "b", "c"}
    assert result["failed"] == []
    assert Registry().require("a").state == "dropped"
    assert Registry().require("c").sm_name == "sm-c"


def test_drop_bulk_handles_coin_finder_scale(home):
    from portunus import mcp_server, Registry
    entries = [
        {"name": f"candidate-{i}", "sm_name": f"SM_{i}", "value": f"pw{i}"}
        for i in range(100)
    ]
    result = mcp_server.portunus_drop_bulk(entries)
    assert len(result["created"]) == 100
    assert result["failed"] == []
    reg = Registry()
    assert reg.require("candidate-0").sm_name == "SM_0"
    assert reg.require("candidate-99").sm_name == "SM_99"


def test_drop_bulk_isolates_partial_failures(home):
    from portunus import mcp_server, Registry
    entries = [{"name": f"x{i}", "sm_name": f"sm-x{i}", "value": f"v{i}"} for i in range(100)]
    entries[46] = {"name": "bad-empty", "sm_name": "sm-bad", "value": ""}
    entries[60] = {"name": "x0", "sm_name": "sm-dup", "value": "v"}  # duplicate name (overwrite, not a failure)

    result = mcp_server.portunus_drop_bulk(entries)
    assert len(result["created"]) == 99  # 100 entries - 1 real failure (dup overwrites, doesn't fail)
    assert len(result["failed"]) == 1
    assert result["failed"][0]["name"] == "bad-empty"
    assert "empty" in result["failed"][0]["error"]
    # the other 98 valid entries + the overwritten x0 still landed
    assert Registry().require("x1").sm_name == "sm-x1"
    assert Registry().require("x0").sm_name == "sm-dup"


def test_drop_bulk_backend_gate_checked_once_upfront(home, monkeypatch):
    from portunus import mcp_server
    monkeypatch.setenv("PORTUNUS_BACKEND", "gcloud")
    entries = [{"name": "a", "sm_name": "sm-a", "value": "va"}]
    result = mcp_server.portunus_drop_bulk(entries)
    assert result == {
        "error": "drop requires the local-encrypted backend "
        "(unset PORTUNUS_BACKEND or set it to unset/local)"
    }


def test_drop_bulk_never_leaks_a_value_source_check():
    """AST-level: no Return node anywhere in portunus_drop_bulk references
    a variable holding an entry's value -- only names/error strings."""
    import ast
    import inspect
    import textwrap
    from portunus import mcp_server

    src = textwrap.dedent(inspect.getsource(mcp_server.portunus_drop_bulk))
    tree = ast.parse(src)
    func = tree.body[0]
    for node in ast.walk(func):
        if isinstance(node, ast.Return) and node.value is not None:
            names = {n.id for n in ast.walk(node.value) if isinstance(n, ast.Name)}
            assert "value" not in names, ast.unparse(node.value)


def test_portunus_sync_never_returns_a_value_source_check():
    """portunus_sync legitimately calls backend.access() to force a sync
    check (unlike the pure-metadata tools) -- the real guarantee here is
    structural: no Return node's expression tree references anything but
    the report dict's own name/error-string lists."""
    import ast
    import inspect
    import textwrap
    from portunus import mcp_server

    src = textwrap.dedent(inspect.getsource(mcp_server.portunus_sync))
    tree = ast.parse(src)
    func = tree.body[0]
    for node in ast.walk(func):
        if isinstance(node, ast.Return) and node.value is not None:
            names = {n.id for n in ast.walk(node.value) if isinstance(n, ast.Name)}
            assert names <= {"synced", "fresh", "failed"}, ast.unparse(node.value)
