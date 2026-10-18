"""portunus gcp add-version (PANT-1171, portunus#231): write a new Secret
Manager version through the project's binding, with a credential preflight,
the value on stdin only -- never argv, stdout/stderr, or the audit log."""
import io
import json
from types import SimpleNamespace

from portunus.audit import AuditChain
from portunus.auth import OIDCToken
from portunus.backend import GcloudBackend, VaultBinding, save_vault_bindings
from portunus.cli import main

SECRET = "re_SUPER_SECRET_VALUE_123"
SA = "portunus@ffe-cicd.iam.gserviceaccount.com"


class StaticOIDC:
    def get(self):
        return OIDCToken(token="OIDC.JWT", issuer="i", subject="agent:hive", audience="a")


def _runner(seen, add_rc=0, mint_ok=True):
    def fake_run(cmd, capture_output=True, text=True, timeout=None, input=None):
        seen.append({"cmd": list(cmd), "input": input})
        if cmd[:3] == ["gcloud", "auth", "print-access-token"]:
            if mint_ok:
                return SimpleNamespace(returncode=0, stdout="ya29.TOKEN", stderr="")
            return SimpleNamespace(returncode=1, stdout="", stderr="Reauthentication failed.")
        if "versions" in cmd and "add" in cmd:
            if add_rc:
                return SimpleNamespace(returncode=add_rc, stdout="", stderr="PERMISSION_DENIED")
            return SimpleNamespace(
                returncode=0, stdout="projects/1/secrets/s/versions/7\n", stderr="",
            )
        raise AssertionError(f"unexpected command {cmd}")
    return fake_run


def _all_output(captured, home):
    audit = (home / "audit.log").read_text() if (home / "audit.log").exists() else ""
    return captured.out + captured.err + audit


def test_backend_add_version_sends_value_on_stdin_never_argv(home, gcloud_on_path):
    seen = []
    backend = GcloudBackend(
        project="ffe-cicd",
        bindings={"ffe-cicd": VaultBinding("ffe-cicd", account="me@example.com",
                                           impersonate_service_account=SA)},
        runner=_runner(seen),
    )
    version = backend.add_version("social-engine-prod-resend-api-key", SECRET, project="ffe-cicd")
    assert version == "projects/1/secrets/s/versions/7"
    call = seen[0]
    assert call["input"] == SECRET
    assert all(SECRET not in arg for arg in call["cmd"])
    assert call["cmd"][:3] == ["gcloud", "--account=me@example.com",
                               f"--impersonate-service-account={SA}"]
    assert "--data-file=-" in call["cmd"]
    assert "--project=ffe-cicd" in call["cmd"]


def test_backend_add_version_with_wif_uses_token_file_only(home, gcloud_on_path):
    seen = []

    def transport(url, data, headers, timeout):
        if "sts.googleapis.com" in url:
            return {"access_token": "FEDERATED", "expires_in": 3600}
        return {"accessToken": "SA.TOKEN", "expireTime": "2030-01-01T00:00:00Z"}

    backend = GcloudBackend(
        bindings={"ffe-cicd": VaultBinding("ffe-cicd", wif_audience="aud",
                                           impersonate_service_account=SA)},
        runner=_runner(seen), audit=AuditChain(),
    )
    provider = backend._binding_providers["ffe-cicd"]
    provider.transport = transport
    provider.token_source = StaticOIDC()
    backend.add_version("s", SECRET, project="ffe-cicd")
    cmd = seen[0]["cmd"]
    assert any(a.startswith("--access-token-file=") for a in cmd)
    assert not any(a.startswith("--account=") for a in cmd)


def test_cli_add_version_from_stdin(home, monkeypatch, capsys, gcloud_on_path):
    save_vault_bindings({"ffe-cicd": VaultBinding("ffe-cicd", account="me@example.com")})
    seen = []
    monkeypatch.setattr("portunus.cli.subprocess.run", _runner(seen))
    monkeypatch.setattr("portunus.backend.subprocess.run", _runner(seen))
    monkeypatch.setattr("sys.stdin", io.StringIO(SECRET + "\n"))

    rc = main(["gcp", "add-version", "ffe-cicd", "social-engine-prod-resend-api-key", "--stdin"])
    captured = capsys.readouterr()
    assert rc == 0
    assert "added version to ffe-cicd/social-engine-prod-resend-api-key" in captured.out
    assert SECRET not in _all_output(captured, home)
    # preflight probe ran before the write
    assert seen[0]["cmd"][:3] == ["gcloud", "auth", "print-access-token"]
    assert seen[-1]["input"] == SECRET
    assert "gcp-add-version" in (home / "audit.log").read_text()


def test_cli_add_version_from_value_file(home, monkeypatch, capsys, gcloud_on_path, tmp_path):
    seen = []
    monkeypatch.setattr("portunus.backend.subprocess.run", _runner(seen))
    value_file = tmp_path / "v.txt"
    value_file.write_text(SECRET + "\n")
    rc = main(["gcp", "add-version", "ffe-cicd", "s", "--value-file", str(value_file)])
    captured = capsys.readouterr()
    assert rc == 0
    assert seen[-1]["input"] == SECRET
    assert SECRET not in _all_output(captured, home)


def test_cli_add_version_preflight_blocks_expired_account(home, monkeypatch, capsys, gcloud_on_path):
    save_vault_bindings({"ffe-cicd": VaultBinding("ffe-cicd", account="me@example.com")})
    seen = []
    monkeypatch.setattr("portunus.cli.subprocess.run", _runner(seen, mint_ok=False))
    monkeypatch.setattr("portunus.backend.subprocess.run", _runner(seen, mint_ok=False))
    monkeypatch.setattr("sys.stdin", io.StringIO(SECRET))

    rc = main(["gcp", "add-version", "ffe-cicd", "s", "--stdin"])
    captured = capsys.readouterr()
    assert rc == 1
    assert "preflight failed" in captured.err
    assert "gcloud auth login me@example.com --no-activate" in captured.err
    assert not any("add" in c["cmd"] and "versions" in c["cmd"] for c in seen)
    assert SECRET not in _all_output(captured, home)


def test_cli_add_version_gcloud_failure_does_not_leak(home, monkeypatch, capsys, gcloud_on_path):
    seen = []
    monkeypatch.setattr("portunus.backend.subprocess.run", _runner(seen, add_rc=1))
    monkeypatch.setattr("sys.stdin", io.StringIO(SECRET))
    rc = main(["gcp", "add-version", "ffe-cicd", "s", "--stdin"])
    captured = capsys.readouterr()
    assert rc == 1
    assert "PERMISSION_DENIED" in captured.err
    assert SECRET not in _all_output(captured, home)


def test_cli_add_version_rejects_empty_and_non_gcp_binding(home, monkeypatch, capsys, gcloud_on_path):
    monkeypatch.setattr("sys.stdin", io.StringIO("   \n"))
    assert main(["gcp", "add-version", "ffe-cicd", "s", "--stdin"]) == 1
    assert "empty" in capsys.readouterr().err

    save_vault_bindings({"loc": VaultBinding("loc", backend="local")})
    monkeypatch.setattr("sys.stdin", io.StringIO(SECRET))
    assert main(["gcp", "add-version", "loc", "s", "--stdin"]) == 1
    assert "backend=local" in capsys.readouterr().err


def test_cli_add_version_requires_a_value_source(home, capsys):
    import pytest
    with pytest.raises(SystemExit):
        main(["gcp", "add-version", "ffe-cicd", "s"])


def test_add_version_value_never_reaches_print_or_audit_structurally():
    """AST check: inside cmd_gcp_add_version, `value` is never an argument
    to print(), _err() or audit.append()."""
    import ast
    import inspect
    import textwrap
    from portunus import cli

    tree = ast.parse(textwrap.dedent(inspect.getsource(cli.cmd_gcp_add_version)))
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call):
            continue
        func = node.func
        name = func.id if isinstance(func, ast.Name) else getattr(func, "attr", "")
        if name in ("print", "_err", "append"):
            names = {n.id for arg in node.args for n in ast.walk(arg) if isinstance(n, ast.Name)}
            assert "value" not in names, f"value flows into {name}()"
