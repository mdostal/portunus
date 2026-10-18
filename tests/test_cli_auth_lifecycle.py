"""portunus auth login/status (story 06, portunus-mcp-server) -- bounded auth
lifecycle: a thin `gcloud auth login` wrapper, and a status report that
cross-references configured bindings against gcloud's credentialed accounts.
Neither ever touches a secret value -- only account emails and gcloud's own
credential metadata."""
import json

from portunus.cli import main


def test_auth_login_invokes_gcloud_and_reports_success(home, monkeypatch, capsys, gcloud_on_path):
    from types import SimpleNamespace
    seen = {}

    def fake_run(cmd, capture_output, text, timeout):
        seen["cmd"] = cmd
        return SimpleNamespace(returncode=0, stdout="You are now logged in.", stderr="")

    monkeypatch.setattr("portunus.cli.subprocess.run", fake_run)

    rc = main(["auth", "login", "user@example.com"])
    out = capsys.readouterr().out
    assert rc == 0
    assert seen["cmd"] == ["gcloud", "auth", "login", "user@example.com"]
    assert "user@example.com" in out


def test_auth_login_reports_failure(home, monkeypatch, capsys, gcloud_on_path):
    from types import SimpleNamespace

    def fake_run(cmd, capture_output, text, timeout):
        return SimpleNamespace(returncode=1, stdout="", stderr="login denied")

    monkeypatch.setattr("portunus.cli.subprocess.run", fake_run)

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


def test_auth_status_reports_per_binding_authenticated_state(home, monkeypatch, capsys, gcloud_on_path):
    from types import SimpleNamespace
    from portunus.backend import VaultBinding, save_vault_bindings

    save_vault_bindings({
        "demo-project-483920": VaultBinding("demo-project-483920", account="personal@example.com"),
        "demo-cicd": VaultBinding("demo-cicd", account="work@example.com"),
    })

    def fake_run(cmd, capture_output, text, timeout):
        if cmd[:3] == ["gcloud", "auth", "print-access-token"]:
            assert cmd[3] == "--account=personal@example.com"
            return SimpleNamespace(returncode=0, stdout="ya29.FAKE", stderr="")
        assert cmd == ["gcloud", "auth", "list", "--format=json"]
        return SimpleNamespace(
            returncode=0,
            stdout=json.dumps([{"account": "personal@example.com", "status": "ACTIVE"}]),
            stderr="",
        )

    monkeypatch.setattr("portunus.cli.subprocess.run", fake_run)

    rc = main(["auth", "status"])
    out = capsys.readouterr().out
    assert rc == 0
    assert "demo-project-483920" in out and "authenticated" in out
    assert "demo-cicd" in out and "MISSING" in out


def test_auth_status_json_shape(home, monkeypatch, capsys, gcloud_on_path):
    from types import SimpleNamespace
    from portunus.backend import VaultBinding, save_vault_bindings

    save_vault_bindings({
        "a": VaultBinding("a", account="one@example.com"),
        "b": VaultBinding("b", account="two@example.com"),
    })

    def fake_run(cmd, capture_output, text, timeout):
        if cmd[:3] == ["gcloud", "auth", "print-access-token"]:
            return SimpleNamespace(returncode=0, stdout="ya29.FAKE", stderr="")
        return SimpleNamespace(
            returncode=0, stdout=json.dumps([{"account": "one@example.com"}]), stderr="",
        )

    monkeypatch.setattr("portunus.cli.subprocess.run", fake_run)

    rc = main(["auth", "status", "--json"])
    out = json.loads(capsys.readouterr().out)
    assert rc == 0
    assert out["a"] == {
        "account": "one@example.com", "authenticated": True,
        "mode": "account", "state": "ok", "impersonate_service_account": "",
    }
    assert out["b"] == {
        "account": "two@example.com", "authenticated": False,
        "mode": "account", "state": "missing", "impersonate_service_account": "",
    }


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


# --- PANT-1171 / portunus#231: flag expired credentials before a write ---------

def _status_runner(monkeypatch, listed, mint_ok, seen=None):
    from types import SimpleNamespace

    def fake_run(cmd, capture_output, text, timeout):
        if seen is not None:
            seen.append(cmd)
        if cmd[:3] == ["gcloud", "auth", "print-access-token"]:
            account = cmd[3].split("=", 1)[1]
            if mint_ok.get(account, False):
                return SimpleNamespace(returncode=0, stdout="ya29.SHOULD-NEVER-PRINT", stderr="")
            return SimpleNamespace(
                returncode=1, stdout="",
                stderr="Reauthentication failed. cannot prompt during non-interactive execution.",
            )
        return SimpleNamespace(
            returncode=0, stdout=json.dumps([{"account": a} for a in listed]), stderr="",
        )

    monkeypatch.setattr("portunus.cli.subprocess.run", fake_run)


def test_auth_status_flags_listed_but_reauth_walled_account_as_expired(
    home, monkeypatch, capsys, gcloud_on_path,
):
    from portunus.backend import VaultBinding, save_vault_bindings

    save_vault_bindings({"ffe-cicd": VaultBinding("ffe-cicd", account="work@example.com")})
    _status_runner(monkeypatch, listed=["work@example.com"], mint_ok={"work@example.com": False})

    rc = main(["auth", "status"])
    out = capsys.readouterr().out
    assert rc == 0
    assert "EXPIRED" in out
    assert "gcloud auth login work@example.com --no-activate" in out


def test_auth_status_strict_exits_nonzero_on_expired(home, monkeypatch, capsys, gcloud_on_path):
    from portunus.backend import VaultBinding, save_vault_bindings

    save_vault_bindings({"ffe-cicd": VaultBinding("ffe-cicd", account="work@example.com")})
    _status_runner(monkeypatch, listed=["work@example.com"], mint_ok={})

    rc = main(["auth", "status", "--strict", "--json"])
    out = json.loads(capsys.readouterr().out)
    assert rc == 1
    assert out["ffe-cicd"]["state"] == "expired"
    assert out["ffe-cicd"]["authenticated"] is False


def test_auth_status_strict_exits_zero_when_everything_usable(home, monkeypatch, capsys, gcloud_on_path):
    from portunus.backend import VaultBinding, save_vault_bindings

    save_vault_bindings({"ffe-cicd": VaultBinding("ffe-cicd", account="work@example.com")})
    _status_runner(monkeypatch, listed=["work@example.com"], mint_ok={"work@example.com": True})

    assert main(["auth", "status", "--strict"]) == 0


def test_auth_status_probe_passes_impersonation_and_never_prints_token(
    home, monkeypatch, capsys, gcloud_on_path,
):
    from portunus.backend import VaultBinding, save_vault_bindings

    save_vault_bindings({"ffe-cicd": VaultBinding(
        "ffe-cicd", account="personal@example.com",
        impersonate_service_account="portunus@ffe-cicd.iam.gserviceaccount.com",
    )})
    seen = []
    _status_runner(monkeypatch, listed=["personal@example.com"],
                   mint_ok={"personal@example.com": True}, seen=seen)

    for argv in (["auth", "status"], ["auth", "status", "--json"]):
        rc = main(argv)
        captured = capsys.readouterr()
        assert rc == 0
        assert "ya29" not in captured.out and "ya29" not in captured.err
    probe = [c for c in seen if c[:3] == ["gcloud", "auth", "print-access-token"]][0]
    assert "--impersonate-service-account=portunus@ffe-cicd.iam.gserviceaccount.com" in probe


def test_auth_status_wif_binding_reports_keyless_ready_without_gcloud(
    home, monkeypatch, capsys,
):
    from portunus.backend import VaultBinding, save_vault_bindings

    save_vault_bindings({"ffe-cicd": VaultBinding(
        "ffe-cicd", wif_audience="//iam.googleapis.com/projects/1/locations/global/"
        "workloadIdentityPools/p/providers/p",
        impersonate_service_account="portunus@ffe-cicd.iam.gserviceaccount.com",
    )})
    monkeypatch.setattr("portunus.cli.shutil.which", lambda name: None)

    def no_subprocess(*a, **k):
        raise AssertionError("WIF status must not shell out to gcloud")

    monkeypatch.setattr("portunus.cli.subprocess.run", no_subprocess)
    monkeypatch.setenv("PORTUNUS_OIDC_TOKEN", "OIDC.JWT.SECRET")

    rc = main(["auth", "status", "--json", "--strict"])
    captured = capsys.readouterr()
    out = json.loads(captured.out)
    assert rc == 0
    assert out["ffe-cicd"]["mode"] == "wif"
    assert out["ffe-cicd"]["state"] == "ready"
    assert "OIDC.JWT.SECRET" not in captured.out + captured.err


def test_auth_status_wif_binding_flags_missing_and_expired_oidc(home, monkeypatch, capsys):
    from portunus.backend import VaultBinding, save_vault_bindings

    save_vault_bindings({"ffe-cicd": VaultBinding("ffe-cicd", wif_audience="aud")})
    monkeypatch.setattr("portunus.cli.shutil.which", lambda name: None)
    monkeypatch.delenv("PORTUNUS_OIDC_TOKEN", raising=False)
    monkeypatch.delenv("PORTUNUS_OIDC_TOKEN_FILE", raising=False)

    rc = main(["auth", "status", "--strict", "--json"])
    assert rc == 1
    assert json.loads(capsys.readouterr().out)["ffe-cicd"]["state"] == "no-oidc-token"

    monkeypatch.setenv("PORTUNUS_OIDC_TOKEN", "OIDC.JWT.SECRET")
    monkeypatch.setenv("PORTUNUS_OIDC_EXPIRES_AT", "1")
    rc = main(["auth", "status", "--strict"])
    captured = capsys.readouterr()
    assert rc == 1
    assert "OIDC TOKEN EXPIRED" in captured.out
    assert "OIDC.JWT.SECRET" not in captured.out + captured.err


def test_auth_status_ambient_binding_is_unchecked_not_a_strict_failure(home, monkeypatch, capsys):
    from portunus.backend import VaultBinding, save_vault_bindings

    save_vault_bindings({"demo": VaultBinding("demo")})
    monkeypatch.setattr("portunus.cli.shutil.which", lambda name: None)
    rc = main(["auth", "status", "--strict", "--json"])
    out = json.loads(capsys.readouterr().out)
    assert rc == 0
    assert out["demo"]["mode"] == "ambient" and out["demo"]["state"] == "unchecked"
