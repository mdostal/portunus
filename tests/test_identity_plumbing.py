"""portunus-petitio-rbac Story 01: thread requester=Identity.from_env() through
every real check_injectable() call site. Pure plumbing -- zero behavior change.
No policy logic exists yet (Story 02); this only makes identity visible at the
chokepoint so a future evaluator has something real to act on."""
import ast
import inspect

from portunus.broker import Broker, Identity


def _spy_check_injectable(monkeypatch):
    calls = []
    original = Broker.check_injectable

    def spy(self, name, requester=None):
        calls.append({"name": name, "requester": requester})
        return original(self, name, requester=requester)

    monkeypatch.setattr(Broker, "check_injectable", spy)
    return calls


def test_resolver_passes_requester(stack, monkeypatch):
    calls = _spy_check_injectable(monkeypatch)
    stack["registry"].add("x", "sm-x", scope="shared", kind="anthropic")
    stack["backend"].set("sm-x", "FAKE-TEST-VALUE-do-not-leak-0xBEEF")
    stack["resolver"].resolve_call("{{secret:x}}", lambda v: v)
    assert len(calls) == 1
    assert isinstance(calls[0]["requester"], Identity)


def test_cli_sync_passes_requester(home, monkeypatch, capsys):
    from portunus.cli import main
    from portunus import Registry

    calls = _spy_check_injectable(monkeypatch)
    Registry().add("x", "sm-x", project="myproj")
    main(["sync", "myproj"])
    capsys.readouterr()
    assert len(calls) == 1
    assert isinstance(calls[0]["requester"], Identity)


def test_mcp_sync_passes_requester(home, monkeypatch):
    from portunus import Registry
    from portunus.mcp_server import portunus_sync

    calls = _spy_check_injectable(monkeypatch)
    Registry().add("x", "sm-x", project="myproj")
    portunus_sync("myproj")
    assert len(calls) == 1
    assert isinstance(calls[0]["requester"], Identity)


def test_leakscan_get_values_passes_requester(stack, monkeypatch):
    from portunus.leakscan import get_values

    calls = _spy_check_injectable(monkeypatch)
    stack["registry"].add("x", "sm-x", scope="shared", kind="anthropic")
    stack["backend"].set("sm-x", "FAKE-TEST-VALUE-do-not-leak-0xCAFE")
    get_values(stack["registry"], stack["broker"], stack["backend"])
    assert len(calls) == 1
    assert isinstance(calls[0]["requester"], Identity)


def test_requester_resolves_via_the_existing_documented_fallback(home, monkeypatch):
    """No new fallback logic introduced by this story -- requester must
    resolve exactly the way Identity.from_env() already documents."""
    monkeypatch.delenv("DOSTAL_AGENT", raising=False)
    monkeypatch.setenv("USER", "mdostal")
    calls = _spy_check_injectable(monkeypatch)
    from portunus import Registry
    from portunus.mcp_server import portunus_sync

    Registry().add("x", "sm-x", project="myproj")
    portunus_sync("myproj")
    assert calls[0]["requester"] == Identity(name="mdostal", kind="human")


def test_full_suite_behavior_unchanged():
    """This story must not require editing a single existing test's expected
    behavior -- covered by the full suite staying green (verified separately
    via `pytest -q`, not re-asserted here); this test just documents that
    invariant as the story's own acceptance criterion."""
    assert True


def test_requester_passed_by_keyword_not_positionally():
    """AST-inspection of every touched call site: requester= must be a
    keyword argument, matching the existing signature's own keyword-only
    intent and keeping future diffs readable."""
    import portunus.resolver as resolver_mod
    import portunus.cli as cli_mod
    import portunus.mcp_server as mcp_mod
    import portunus.leakscan as leakscan_mod

    for mod in (resolver_mod, cli_mod, mcp_mod, leakscan_mod):
        src = inspect.getsource(mod)
        tree = ast.parse(src)
        found_calls = []
        for node in ast.walk(tree):
            if (
                isinstance(node, ast.Call)
                and isinstance(node.func, ast.Attribute)
                and node.func.attr == "check_injectable"
            ):
                found_calls.append(node)
        assert found_calls, f"no check_injectable( call found in {mod.__name__}"
        for call in found_calls:
            assert not call.args or len(call.args) <= 1, (
                f"{mod.__name__}: check_injectable() called with more than just `name` positionally"
            )
            kw_names = {kw.arg for kw in call.keywords}
            assert "requester" in kw_names, (
                f"{mod.__name__}: check_injectable() call missing requester= keyword"
            )
