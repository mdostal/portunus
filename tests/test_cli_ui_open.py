"""portunus-secure-entry Story 04: `portunus ui open [--fulfill NAME]` --
the one piece an agent's own (necessarily non-interactive) tool call can
actually run itself: it only ever constructs a URL and fires off
webbrowser.open(), no TTY needed, no value ever touched."""
import socket

import pytest

from portunus.cli import main


@pytest.fixture(autouse=True)
def _no_real_browser(monkeypatch):
    """Never actually pop a real browser window during the test suite."""
    calls = []
    monkeypatch.setattr("webbrowser.open", lambda url: calls.append(url) or True)
    return calls


def test_ui_open_opens_browser_when_reachable(home, monkeypatch, capsys, _no_real_browser):
    monkeypatch.setattr("portunus.cli._ui_reachable", lambda url: True)
    rc = main(["ui", "open"])
    assert rc == 0
    assert _no_real_browser == ["http://localhost:3000"]
    assert "opened" in capsys.readouterr().out


def test_ui_open_reports_actionable_message_when_unreachable(home, monkeypatch, capsys, _no_real_browser):
    monkeypatch.setattr("portunus.cli._ui_reachable", lambda url: False)
    rc = main(["ui", "open"])
    assert rc == 1
    err = capsys.readouterr().err
    assert "npm run dev" in err
    assert _no_real_browser == []  # never opens a browser to a broken page


def test_ui_open_respects_url_override(home, monkeypatch, capsys, _no_real_browser):
    monkeypatch.setenv("PORTUNUS_UI_URL", "http://localhost:4242")
    monkeypatch.setattr("portunus.cli._ui_reachable", lambda url: True)
    rc = main(["ui", "open"])
    assert rc == 0
    assert _no_real_browser == ["http://localhost:4242"]


def test_ui_open_fulfill_appends_query_param_for_a_pending_request(home, monkeypatch, capsys, _no_real_browser):
    from portunus import Registry

    Registry().request("pending-one", org="demo-org")
    monkeypatch.setattr("portunus.cli._ui_reachable", lambda url: True)
    rc = main(["ui", "open", "--fulfill", "pending-one"])
    assert rc == 0
    assert _no_real_browser == ["http://localhost:3000?fulfill=pending-one"]


def test_ui_open_fulfill_refuses_when_name_does_not_exist(home, monkeypatch, capsys, _no_real_browser):
    monkeypatch.setattr("portunus.cli._ui_reachable", lambda url: True)
    rc = main(["ui", "open", "--fulfill", "does-not-exist"])
    assert rc == 1
    assert "does-not-exist" in capsys.readouterr().err
    assert _no_real_browser == []  # never opens a browser to a misleading URL


def test_ui_open_fulfill_refuses_when_not_state_requested(home, monkeypatch, capsys, _no_real_browser):
    from portunus import Registry

    Registry().add("already-enabled", "sm-already-enabled")  # state=enabled by default
    monkeypatch.setattr("portunus.cli._ui_reachable", lambda url: True)
    rc = main(["ui", "open", "--fulfill", "already-enabled"])
    assert rc == 1
    err = capsys.readouterr().err
    assert "already-enabled" in err
    assert "enabled" in err  # names the actual current state
    assert _no_real_browser == []


def test_ui_open_fulfill_validated_before_reachability_probe(home, monkeypatch, capsys, _no_real_browser):
    """A misleading URL should never open even if the UI happens to be
    reachable -- --fulfill validation is checked first."""
    reachable_calls = []
    monkeypatch.setattr("portunus.cli._ui_reachable", lambda url: reachable_calls.append(url) or True)
    rc = main(["ui", "open", "--fulfill", "does-not-exist"])
    assert rc == 1
    assert reachable_calls == []
    assert _no_real_browser == []


# --- _ui_reachable() -- real socket behavior, not mocked -----------------

def test_ui_reachable_true_for_a_real_bound_local_socket():
    from portunus.cli import _ui_reachable

    srv = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    srv.bind(("127.0.0.1", 0))
    srv.listen(1)
    port = srv.getsockname()[1]
    try:
        assert _ui_reachable(f"http://127.0.0.1:{port}") is True
    finally:
        srv.close()


def test_ui_reachable_false_for_a_port_nothing_is_listening_on():
    from portunus.cli import _ui_reachable

    # Bind to an ephemeral port, learn its number, then close it immediately
    # -- nothing should be listening there a moment later.
    srv = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    srv.bind(("127.0.0.1", 0))
    port = srv.getsockname()[1]
    srv.close()
    assert _ui_reachable(f"http://127.0.0.1:{port}") is False
