"""`discover --register` eager sync-down (portunus-vault-backup story 01).

A freshly-registered reference under a sync_mode=cached project gets its
local cache warmed immediately, reusing SyncingBackend exactly as `cmd_sync`
does -- deliberately bypassing Broker.check_injectable for this one internal
cache-populate operation (design-discussion.md §4b). The reference's `state`
stays "requested" throughout: every real resolve/inject/ask/MCP path remains
fully fail-closed, unaffected by whether the cache happens to be warm.
"""
import ast
import inspect
import json
import shutil
import subprocess
import textwrap
from types import SimpleNamespace

import pytest

from portunus import Registry
from portunus.backend import VaultBinding, save_vault_bindings
from portunus.broker import NotInjectable
from portunus.cli import _eager_sync_down, main


def _mock_gcloud(monkeypatch, list_secrets, backend_responses=None, fail_on_backend_call=False):
    """discover.py and backend.py both `import subprocess`/`import shutil` --
    the SAME module objects, so one patch covers both call sites. `list_secrets`
    answers every `gcloud secrets list` call; `backend_responses` (a list of
    (rc, out, err) tuples, consumed in order) answers every other gcloud
    invocation (describe/access), unless `fail_on_backend_call` is set, in
    which case any non-list call raises -- used to prove eager sync-down
    never touches the backend for a non-cached-mode project."""
    monkeypatch.setattr(shutil, "which", lambda name: "/bin/gcloud")
    responses = list(backend_responses or [])

    def fake_run(cmd, capture_output, text, timeout):
        if "list" in cmd:
            return SimpleNamespace(returncode=0, stdout=json.dumps(list_secrets), stderr="")
        if fail_on_backend_call:
            raise AssertionError(f"backend must not be called: {cmd}")
        rc, out, err = responses.pop(0)
        return SimpleNamespace(returncode=rc, stdout=out, stderr=err)

    monkeypatch.setattr(subprocess, "run", fake_run)


def test_discover_register_warms_cache_for_cached_mode_project(home, monkeypatch, capsys):
    _mock_gcloud(
        monkeypatch,
        [{"name": "projects/demo/secrets/API_KEY", "labels": {}, "createTime": "T0"}],
        backend_responses=[
            (0, json.dumps({"name": "v1", "createTime": "T1"}), ""),  # describe (latest_version)
            (0, "SECRET-VALUE", ""),                                    # access (real fetch)
        ],
    )
    save_vault_bindings({"demo": VaultBinding("demo", backend="gcp", sync_mode="cached")})

    rc = main(["discover", "--provider", "gcp", "--project", "demo", "--register", "--json"])
    data = json.loads(capsys.readouterr().out)
    assert rc == 0
    assert data["registered"] == ["demo-api_key"]
    assert data["sync_results"] == {"demo-api_key": "synced"}

    from portunus.localvault import LocalEncryptedBackend
    assert LocalEncryptedBackend().access("demo:API_KEY") == "SECRET-VALUE"


def test_discover_register_no_eager_sync_for_direct_mode_project(home, monkeypatch, capsys):
    _mock_gcloud(
        monkeypatch,
        [{"name": "projects/demo/secrets/API_KEY", "labels": {}, "createTime": "T0"}],
        fail_on_backend_call=True,
    )
    save_vault_bindings({"demo": VaultBinding("demo", backend="gcp", sync_mode="direct")})

    rc = main(["discover", "--provider", "gcp", "--project", "demo", "--register", "--json"])
    data = json.loads(capsys.readouterr().out)
    assert rc == 0
    assert data["registered"] == ["demo-api_key"]
    assert data["sync_results"] == {}


def test_discover_register_no_eager_sync_without_any_binding(home, monkeypatch, capsys):
    _mock_gcloud(
        monkeypatch,
        [{"name": "projects/demo/secrets/API_KEY", "labels": {}, "createTime": "T0"}],
        fail_on_backend_call=True,
    )

    rc = main(["discover", "--provider", "gcp", "--project", "demo", "--register", "--json"])
    data = json.loads(capsys.readouterr().out)
    assert rc == 0
    assert data["registered"] == ["demo-api_key"]
    assert data["sync_results"] == {}


def test_discover_register_sync_failure_is_per_reference_and_non_fatal(home, monkeypatch, capsys):
    _mock_gcloud(
        monkeypatch,
        [
            {"name": "projects/demo/secrets/A", "labels": {}, "createTime": "T0"},
            {"name": "projects/demo/secrets/B", "labels": {}, "createTime": "T0"},
        ],
        backend_responses=[
            (0, json.dumps({"name": "v1", "createTime": "T1"}), ""),  # A: describe ok
            (1, "", "gcloud: permission denied"),                      # A: fetch fails
            (0, json.dumps({"name": "v1", "createTime": "T1"}), ""),  # B: describe ok
            (0, "VALUE-B", ""),                                         # B: fetch ok
        ],
    )
    save_vault_bindings({"demo": VaultBinding("demo", backend="gcp", sync_mode="cached")})

    rc = main(["discover", "--provider", "gcp", "--project", "demo", "--register", "--json"])
    data = json.loads(capsys.readouterr().out)
    assert rc == 0
    assert set(data["registered"]) == {"demo-a", "demo-b"}
    assert "sync-failed" in data["sync_results"]["demo-a"]
    assert "permission denied" in data["sync_results"]["demo-a"]
    assert data["sync_results"]["demo-b"] == "synced"

    # Registration itself is unaffected -- both references exist regardless
    # of whether the eager cache-warm succeeded.
    reg = Registry()
    assert reg.require("demo-a").state == "requested"
    assert reg.require("demo-b").state == "requested"


def test_eager_sync_down_does_not_affect_injectability(home, monkeypatch, capsys):
    _mock_gcloud(
        monkeypatch,
        [{"name": "projects/demo/secrets/API_KEY", "labels": {}, "createTime": "T0"}],
        backend_responses=[
            (0, json.dumps({"name": "v1", "createTime": "T1"}), ""),
            (0, "SECRET-VALUE", ""),
        ],
    )
    save_vault_bindings({"demo": VaultBinding("demo", backend="gcp", sync_mode="cached")})

    main(["discover", "--provider", "gcp", "--project", "demo", "--register", "--json"])
    capsys.readouterr()

    from portunus import AuditChain, Broker
    broker = Broker(Registry(), AuditChain())
    with pytest.raises(NotInjectable):
        broker.check_injectable("demo-api_key")


def test_eager_sync_down_never_returns_a_value_source_check():
    """AST-level: the only names a Return expression may reference are the
    function's own metadata dict/loop variables -- never anything holding a
    fetched secret value."""
    src = textwrap.dedent(inspect.getsource(_eager_sync_down))
    tree = ast.parse(src)
    func = tree.body[0]
    for node in ast.walk(func):
        if isinstance(node, ast.Return) and node.value is not None:
            names = {n.id for n in ast.walk(node.value) if isinstance(n, ast.Name)}
            assert names <= {"results"}, ast.unparse(node.value)


def test_eager_sync_down_never_assigns_the_access_result_to_a_variable():
    """Structural: `.access(` is never the right-hand side of an assignment
    in _eager_sync_down -- the fetched value is never captured into anything
    that could later be returned, printed, or logged."""
    src = textwrap.dedent(inspect.getsource(_eager_sync_down))
    tree = ast.parse(src)
    for node in ast.walk(tree):
        if isinstance(node, ast.Assign):
            value = node.value
            if isinstance(value, ast.Call) and isinstance(value.func, ast.Attribute):
                assert value.func.attr != "access", ast.dump(node)


def test_mcp_portunus_discover_register_warms_cache_for_cached_mode_project(home, monkeypatch):
    from portunus import mcp_server

    _mock_gcloud(
        monkeypatch,
        [{"name": "projects/demo/secrets/API_KEY", "labels": {}, "createTime": "T0"}],
        backend_responses=[
            (0, json.dumps({"name": "v1", "createTime": "T1"}), ""),
            (0, "SECRET-VALUE", ""),
        ],
    )
    save_vault_bindings({"demo": VaultBinding("demo", backend="gcp", sync_mode="cached")})

    result = mcp_server.portunus_discover("demo", register=True)
    assert result["registered"] == ["demo-api_key"]
    assert result["sync_results"] == {"demo-api_key": "synced"}


def test_mcp_portunus_discover_register_no_eager_sync_for_direct_mode(home, monkeypatch):
    from portunus import mcp_server

    _mock_gcloud(
        monkeypatch,
        [{"name": "projects/demo/secrets/API_KEY", "labels": {}, "createTime": "T0"}],
        fail_on_backend_call=True,
    )
    save_vault_bindings({"demo": VaultBinding("demo", backend="gcp", sync_mode="direct")})

    result = mcp_server.portunus_discover("demo", register=True)
    assert result["registered"] == ["demo-api_key"]
    assert result["sync_results"] == {}
