"""portunus auth login/status (story 06, portunus-mcp-server) -- bounded auth
lifecycle: a thin `gcloud auth login` wrapper, and a status report that
cross-references configured bindings against gcloud's credentialed accounts.
Neither ever touches a secret value -- only account emails and gcloud's own
credential metadata."""
import json

from portunus.cli import main


def test_auth_login_invokes_gcloud_and_reports_success(home, monkeypatch, capsys):
    from types import SimpleNamespace
    seen = {}

    def fake_run(cmd, capture_output, text, timeout):
        seen["cmd"] = cmd
        return SimpleNamespace(returncode=0, stdout="You are now logged in.", stderr="")

    monkeypatch.setattr("portunus.cli.subprocess.run", fake_run)
    monkeypatch.setattr("portunus.cli.shutil.which", lambda name: "/bin/gcloud")

    rc = main(["auth", "login", "user@example.com"])
    out = capsys.readouterr().out
    assert rc == 0
    assert seen["cmd"] == ["gcloud", "auth", "login", "user@example.com"]
    assert "user@example.com" in out


def test_auth_login_reports_failure(home, monkeypatch, capsys):
    from types import SimpleNamespace

    def fake_run(cmd, capture_output, text, timeout):
        return SimpleNamespace(returncode=1, stdout="", stderr="login denied")

    monkeypatch.setattr("portunus.cli.subprocess.run", fake_run)
    monkeypatch.setattr("portunus.cli.shutil.which", lambda name: "/bin/gcloud")

    rc = main(["auth", "login", "user@example.com"])
    err = capsys.readouterr().err
    assert rc == 1
    assert "login denied" in err


def test_auth_login_no_gcloud_on_path(home, monkeypatch, capsys):
    monkeypatch.setattr("portunus.cli.shutil.which", lambda name: None)
    rc = main(["auth", "login", "user@example.com"])
    err = capsys.readouterr().err
    assert rc == 1
    assert "gcloud" in err


def test_auth_status_reports_per_binding_authenticated_state(home, monkeypatch, capsys):
    from types import SimpleNamespace
    from portunus.backend import GcpProjectBinding, save_gcp_bindings

    save_gcp_bindings({
        "personalsites-487021": GcpProjectBinding("personalsites-487021", account="mathew.dostal@gmail.com"),
        "ffe-cicd": GcpProjectBinding("ffe-cicd", account="mdostal@ff.events"),
    })

    def fake_run(cmd, capture_output, text, timeout):
        assert cmd == ["gcloud", "auth", "list", "--format=json"]
        return SimpleNamespace(
            returncode=0,
            stdout=json.dumps([{"account": "mathew.dostal@gmail.com", "status": "ACTIVE"}]),
            stderr="",
        )

    monkeypatch.setattr("portunus.cli.subprocess.run", fake_run)
    monkeypatch.setattr("portunus.cli.shutil.which", lambda name: "/bin/gcloud")

    rc = main(["auth", "status"])
    out = capsys.readouterr().out
    assert rc == 0
    assert "personalsites-487021" in out and "authenticated" in out
    assert "ffe-cicd" in out and "MISSING" in out


def test_auth_status_json_shape(home, monkeypatch, capsys):
    from types import SimpleNamespace
    from portunus.backend import GcpProjectBinding, save_gcp_bindings

    save_gcp_bindings({
        "a": GcpProjectBinding("a", account="one@example.com"),
        "b": GcpProjectBinding("b", account="two@example.com"),
    })

    def fake_run(cmd, capture_output, text, timeout):
        return SimpleNamespace(
            returncode=0, stdout=json.dumps([{"account": "one@example.com"}]), stderr="",
        )

    monkeypatch.setattr("portunus.cli.subprocess.run", fake_run)
    monkeypatch.setattr("portunus.cli.shutil.which", lambda name: "/bin/gcloud")

    rc = main(["auth", "status", "--json"])
    out = json.loads(capsys.readouterr().out)
    assert rc == 0
    assert out["a"] == {"account": "one@example.com", "authenticated": True}
    assert out["b"] == {"account": "two@example.com", "authenticated": False}


def test_auth_status_no_bindings_configured_is_clean(home, capsys):
    rc = main(["auth", "status"])
    out = capsys.readouterr().out
    assert rc == 0
    assert "no bindings configured" in out


def test_auth_status_no_bindings_configured_json(home, capsys):
    rc = main(["auth", "status", "--json"])
    out = json.loads(capsys.readouterr().out)
    assert rc == 0
    assert out == {}
