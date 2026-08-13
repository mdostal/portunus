"""Keyless WIF/OIDC auth: short-lived token exchange and token-free audit.

Scope note: this module tests auth.py in isolation (story 02). GcloudBackend's
integration with a credential_provider (mint-token-to-tempfile) is story 03's
scope -- see tests/test_backend_gcp_wif.py.
"""
import pytest

from portunus import (
    AWSWebIdentityAuth,
    AuditChain,
    AuthError,
    EnvOIDCTokenSource,
    GCPWorkloadIdentityAuth,
    OIDCToken,
    assert_no_long_lived_cloud_keys,
)


class StaticOIDC:
    def __init__(self, token="OIDC.JWT.TEST"):
        self.token = OIDCToken(
            token=token,
            issuer="https://issuer.example",
            subject="agent:dostal-dev",
            audience="portunus",
            expires_at=0,
        )

    def get(self):
        return self.token


def test_env_oidc_source_redacts_token_from_repr(home, monkeypatch):
    monkeypatch.setenv("PORTUNUS_OIDC_TOKEN", "OIDC.SECRET.VALUE")
    monkeypatch.setenv("PORTUNUS_OIDC_ISSUER", "https://issuer.example")
    monkeypatch.setenv("PORTUNUS_OIDC_SUBJECT", "agent:dostal-dev")
    monkeypatch.setenv("PORTUNUS_OIDC_AUDIENCE", "portunus")
    token = EnvOIDCTokenSource().get()
    assert token.token == "OIDC.SECRET.VALUE"
    assert "OIDC.SECRET.VALUE" not in repr(token)


def test_gcp_wif_exchange_is_scoped_and_audited_without_token(home):
    seen = {}

    def transport(url, data, headers, timeout):
        seen["url"] = url
        seen["data"] = dict(data)
        return {"access_token": "GCP.ACCESS.TOKEN", "expires_in": 900}

    auth = GCPWorkloadIdentityAuth(
        audience="//iam.googleapis.com/projects/1/locations/global/workloadIdentityPools/p/providers/p",
        token_source=StaticOIDC(),
        audit=AuditChain(),
        transport=transport,
    )
    minted = auth.mint()
    assert minted.access_token == "GCP.ACCESS.TOKEN"
    assert seen["data"]["subject_token"] == "OIDC.JWT.TEST"
    assert seen["data"]["requested_token_type"].endswith("access_token")
    audit_text = (home / "audit.log").read_text()
    assert "OIDC.JWT.TEST" not in audit_text
    assert "GCP.ACCESS.TOKEN" not in audit_text
    assert "ok:gcp-wif" in audit_text


def test_aws_web_identity_exchange_is_audited_without_token(home):
    def transport(url, data, timeout):
        assert data["WebIdentityToken"] == "OIDC.JWT.TEST"
        return """<AssumeRoleWithWebIdentityResponse xmlns="https://sts.amazonaws.com/doc/2011-06-15/">
          <AssumeRoleWithWebIdentityResult><Credentials>
            <AccessKeyId>ASIA_TEST</AccessKeyId>
            <SecretAccessKey>AWS_SECRET</SecretAccessKey>
            <SessionToken>AWS_SESSION</SessionToken>
          </Credentials></AssumeRoleWithWebIdentityResult>
        </AssumeRoleWithWebIdentityResponse>"""

    auth = AWSWebIdentityAuth(
        role_arn="arn:aws:iam::123456789012:role/portunus-agent",
        token_source=StaticOIDC(),
        audit=AuditChain(),
        transport=transport,
    )
    creds = auth.mint()
    assert creds.access_key_id == "ASIA_TEST"
    audit_text = (home / "audit.log").read_text()
    assert "OIDC.JWT.TEST" not in audit_text
    assert "AWS_SECRET" not in audit_text
    assert "AWS_SESSION" not in audit_text
    assert "ok:aws-web-identity" in audit_text


def test_long_lived_cloud_key_conformance_rejects_static_inputs(tmp_path):
    with pytest.raises(AuthError):
        assert_no_long_lived_cloud_keys(env={"AWS_ACCESS_KEY_ID": "AKIA..."})
    key_file = tmp_path / "sa.json"
    key_file.write_text('{"type": "service_account", "private_key": "SECRET"}')
    with pytest.raises(AuthError):
        assert_no_long_lived_cloud_keys(env={}, paths={"sa": key_file})
