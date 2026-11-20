"""Account profiles (several logins per provider, one active) and the MCP headersHelper sink."""
import ast
import inspect
import io
import json
import stat
import types

import pytest

from portunus import profiles
from portunus.profiles import HeadersRefused, ProfileError

SECRET = "phx_FAKE-TEST-VALUE-do-not-leak-0xBEEF"


class Pipe(io.StringIO):
    def isatty(self):
        return False


class Tty(io.StringIO):
    def isatty(self):
        return True


ENV = {"CLAUDE_CODE_MCP_SERVER_NAME": "posthog"}


def _gcloud(accounts, calls, set_rc=0):
    def run(argv, **kwargs):
        calls.append(argv)
        if argv[:3] == ["gcloud", "auth", "list"]:
            return types.SimpleNamespace(returncode=0, stdout=json.dumps([{"account": a} for a in accounts]), stderr="")
        return types.SimpleNamespace(returncode=set_rc, stdout="", stderr="boom" if set_rc else "")
    return run


def _posthog(stack, name="mathew", ref="posthog-mathew"):
    stack["registry"].add(ref, f"sm-{ref}", kind="api_key", provider="posthog")
    stack["backend"].set(f"sm-{ref}", SECRET)
    return profiles.add_profile("posthog", name, ref=ref, known_ref=lambda r: r in stack["registry"])


# --- the store ------------------------------------------------------------

def test_first_profile_becomes_active_and_a_second_does_not(home):
    profiles.add_profile("gcloud", "personal", account="me@example.com")
    profiles.add_profile("gcloud", "work", account="me@work.example")
    assert profiles.active_profile("gcloud").name == "personal"
    assert set(profiles.load_profiles()["gcloud"].profiles) == {"personal", "work"}


def test_store_is_0600_and_holds_names_only(stack, home):
    _posthog(stack)
    path = home / "profiles.json"
    assert stat.S_IMODE(path.stat().st_mode) == 0o600
    text = path.read_text()
    assert SECRET not in text
    assert json.loads(text)["posthog"]["profiles"]["mathew"]["ref"] == "posthog-mathew"


def test_posthog_profile_must_point_at_a_real_reference(home):
    with pytest.raises(ProfileError, match="no vault reference"):
        profiles.add_profile("posthog", "x", ref="missing", known_ref=lambda r: False)


def test_unknown_provider_and_missing_fields_are_refused(home):
    with pytest.raises(ProfileError, match="unknown provider"):
        profiles.add_profile("aws", "x", account="a")
    with pytest.raises(ProfileError, match="--account"):
        profiles.add_profile("gcloud", "x")
    with pytest.raises(ProfileError, match="--ref"):
        profiles.add_profile("posthog", "x")


def test_removing_the_active_profile_clears_active(home):
    profiles.add_profile("gcloud", "personal", account="me@example.com")
    profiles.remove_profile("gcloud", "personal")
    assert profiles.active_profile("gcloud") is None


# --- swapping -------------------------------------------------------------

def test_use_gcloud_switches_gclouds_active_account(home):
    profiles.add_profile("gcloud", "personal", account="me@example.com")
    profiles.add_profile("gcloud", "work", account="me@work.example")
    calls = []
    profiles.use_profile("gcloud", "work", runner=_gcloud(["me@example.com", "me@work.example"], calls))
    assert ["gcloud", "config", "set", "account", "me@work.example"] in calls
    assert profiles.active_profile("gcloud").name == "work"


def test_use_gcloud_refuses_an_account_that_is_not_logged_in(home):
    profiles.add_profile("gcloud", "work", account="me@work.example")
    calls = []
    with pytest.raises(ProfileError, match="portunus auth login me@work.example"):
        profiles.use_profile("gcloud", "work", runner=_gcloud(["me@example.com"], calls))
    assert not any(c[:3] == ["gcloud", "config", "set"] for c in calls)


def test_use_gcloud_keeps_the_old_active_profile_when_gcloud_fails(home):
    profiles.add_profile("gcloud", "personal", account="me@example.com")
    profiles.add_profile("gcloud", "work", account="me@work.example")
    with pytest.raises(ProfileError, match="config set account failed"):
        profiles.use_profile("gcloud", "work", runner=_gcloud(["me@example.com", "me@work.example"], [], set_rc=1))
    assert profiles.active_profile("gcloud").name == "personal"


def test_use_posthog_only_records_the_active_profile(stack, home):
    _posthog(stack, "mathew", "posthog-mathew")
    _posthog(stack, "me", "posthog-me")
    profiles.use_profile("posthog", "me", runner=lambda *a, **k: pytest.fail("posthog must not run a command"))
    assert profiles.active_profile("posthog").ref == "posthog-me"


def test_use_an_unknown_profile_names_the_ones_that_exist(home):
    profiles.add_profile("gcloud", "personal", account="me@example.com")
    with pytest.raises(ProfileError, match="have: personal"):
        profiles.use_profile("gcloud", "nope")


# --- the headersHelper sink -----------------------------------------------

def test_headers_go_to_the_pipe_as_json_for_the_active_profile(stack, home):
    _posthog(stack, "mathew", "posthog-mathew")
    out = Pipe()
    profiles.emit_mcp_headers("posthog", stack["resolver"], out, env=ENV)
    assert json.loads(out.getvalue()) == {"Authorization": f"Bearer {SECRET}"}


def test_headers_follow_a_swap(stack, home):
    _posthog(stack, "mathew", "posthog-mathew")
    stack["registry"].add("posthog-me", "sm-posthog-me", kind="api_key")
    stack["backend"].set("sm-posthog-me", "phx_OTHER")
    profiles.add_profile("posthog", "me", ref="posthog-me")
    profiles.use_profile("posthog", "me")
    out = Pipe()
    profiles.emit_mcp_headers("posthog", stack["resolver"], out, env=ENV)
    assert json.loads(out.getvalue())["Authorization"] == "Bearer phx_OTHER"


def test_headers_refuse_a_terminal(stack, home):
    _posthog(stack)
    out = Tty()
    with pytest.raises(HeadersRefused, match="terminal"):
        profiles.emit_mcp_headers("posthog", stack["resolver"], out, env=ENV)
    assert out.getvalue() == ""


def test_headers_refuse_outside_claude_codes_headers_helper(stack, home):
    _posthog(stack)
    out = Pipe()
    with pytest.raises(HeadersRefused, match="CLAUDE_CODE_MCP_SERVER_NAME"):
        profiles.emit_mcp_headers("posthog", stack["resolver"], out, env={})
    assert out.getvalue() == ""


def test_headers_need_an_active_profile(stack, home):
    with pytest.raises(ProfileError, match="no active posthog profile"):
        profiles.emit_mcp_headers("posthog", stack["resolver"], Pipe(), env=ENV)


def test_gcloud_has_no_headers(stack, home):
    with pytest.raises(ProfileError, match="no MCP headers"):
        profiles.emit_mcp_headers("gcloud", stack["resolver"], Pipe(), env=ENV)


def test_a_value_that_breaks_the_json_never_appears_in_the_error(stack, home):
    stack["registry"].add("bad", "sm-bad", kind="api_key")
    stack["backend"].set("sm-bad", 'has"quote' + SECRET)
    profiles.add_profile("posthog", "bad", ref="bad")
    out = Pipe()
    with pytest.raises(ProfileError) as exc:
        profiles.emit_mcp_headers("posthog", stack["resolver"], out, env=ENV)
    assert SECRET not in str(exc.value)
    assert out.getvalue() == ""


def test_headers_are_audited_by_name_never_value(stack, home):
    _posthog(stack, "mathew", "posthog-mathew")
    profiles.emit_mcp_headers("posthog", stack["resolver"], Pipe(), env=ENV)
    log = "".join(p.read_text() for p in home.rglob("*") if p.is_file() and p.name != "profiles.json")
    assert "mcp-headers" in log and "posthog:mathew" in log
    assert SECRET not in log


def test_only_the_sink_writes_to_out():
    """Structural: inside emit_mcp_headers, `out.write` happens only in the nested sink the resolver calls."""
    tree = ast.parse(inspect.getsource(profiles.emit_mcp_headers).lstrip())
    fn = tree.body[0]
    sinks = [n for n in fn.body if isinstance(n, ast.FunctionDef)]
    assert [s.name for s in sinks] == ["sink"]
    outside = [n for n in fn.body if not isinstance(n, ast.FunctionDef)]
    for node in outside:
        for sub in ast.walk(node):
            if isinstance(sub, ast.Attribute) and sub.attr == "write":
                pytest.fail("emit_mcp_headers writes outside its boundary sink")


# --- the CLI, end to end ----------------------------------------------------

def test_cli_add_use_and_headers_through_a_real_pipe(home, monkeypatch):
    """Two PostHog accounts in the vault, swapped with `profile use`, read by `mcp-headers` the way Claude Code does:
    a child process whose stdout is a pipe and which has CLAUDE_CODE_MCP_SERVER_NAME set."""
    import os
    import subprocess
    import sys

    from portunus import Registry
    from portunus.cli import main

    reg = Registry()
    reg.add("ph-mathew", "ph-mathew-key", kind="api_key")
    reg.add("ph-me", "ph-me-key", kind="api_key")
    assert main(["profile", "add", "posthog", "mathew", "--ref", "ph-mathew"]) == 0
    assert main(["profile", "add", "posthog", "me", "--ref", "ph-me"]) == 0
    assert main(["profile", "add", "posthog", "ghost", "--ref", "nope"]) == 1

    env = dict(os.environ, PORTUNUS_BACKEND="mock", PORTUNUS_MOCK_PH_MATHEW_KEY="phx_mathew", PORTUNUS_MOCK_PH_ME_KEY="phx_me", CLAUDE_CODE_MCP_SERVER_NAME="posthog")

    def headers():
        proc = subprocess.run([sys.executable, "-m", "portunus", "mcp-headers", "posthog"], capture_output=True, text=True, env=env)
        assert proc.returncode == 0, proc.stderr
        return json.loads(proc.stdout)["Authorization"]

    assert headers() == "Bearer phx_mathew"
    assert main(["profile", "use", "posthog", "me"]) == 0
    assert headers() == "Bearer phx_me"

    env.pop("CLAUDE_CODE_MCP_SERVER_NAME")
    proc = subprocess.run([sys.executable, "-m", "portunus", "mcp-headers", "posthog"], capture_output=True, text=True, env=env)
    assert proc.returncode == 1 and proc.stdout == "" and "phx_" not in proc.stderr


def test_cli_list_shows_names_and_the_active_marker(home, capsys):
    from portunus.cli import main

    main(["profile", "add", "gcloud", "personal", "--account", "me@example.com"])
    main(["profile", "add", "gcloud", "work", "--account", "me@work.example", "--note", "ff.events"])
    capsys.readouterr()
    assert main(["profile", "list", "gcloud"]) == 0
    out = capsys.readouterr().out
    assert "* personal" in out and "  work" in out and "ff.events" in out
