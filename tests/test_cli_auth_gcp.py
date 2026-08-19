"""portunus auth gcp -- mints a WIF token, reports identity/scope/expiry, never the token."""
from portunus.cli import main


def _mock_mint(monkeypatch, access_token="GCP.SECRET.TOKEN"):
    def fake_mint(self):
        from portunus.auth import GCPAccessToken
        return GCPAccessToken(
            access_token=access_token, expires_at=1234567890,
            identity="agent:dostal-dev", scope=self.scope,
        )
    monkeypatch.setattr("portunus.cli.GCPWorkloadIdentityAuth.mint", fake_mint)


def test_auth_gcp_prints_identity_never_the_token(home, monkeypatch, capsys):
    monkeypatch.setenv("PORTUNUS_OIDC_TOKEN", "OIDC.JWT.TEST")
    _mock_mint(monkeypatch)

    rc = main(["auth", "gcp", "--project", "demo-project-483920", "--audience", "aud"])
    out = capsys.readouterr().out
    assert rc == 0
    assert "identity=agent:dostal-dev" in out
    assert "expires_at=1234567890" in out
    assert "GCP.SECRET.TOKEN" not in out


def test_auth_gcp_resolves_audience_from_bindings_when_not_passed(home, monkeypatch, capsys):
    from portunus.backend import VaultBinding, save_vault_bindings
    save_vault_bindings({"demo-project-483920": VaultBinding("demo-project-483920", "aud-from-file")})
    monkeypatch.setenv("PORTUNUS_OIDC_TOKEN", "OIDC.JWT.TEST")

    seen = {}
    def fake_mint(self):
        seen["audience"] = self.audience
        from portunus.auth import GCPAccessToken
        return GCPAccessToken(access_token="X", expires_at=1, identity="i", scope=self.scope)
    monkeypatch.setattr("portunus.cli.GCPWorkloadIdentityAuth.mint", fake_mint)

    rc = main(["auth", "gcp", "--project", "demo-project-483920"])
    assert rc == 0
    assert seen["audience"] == "aud-from-file"


def test_auth_gcp_fails_closed_with_no_token(home, capsys):
    rc = main(["auth", "gcp", "--project", "p", "--audience", "aud"])
    err = capsys.readouterr().err
    assert rc == 1
    assert "OIDC" in err
