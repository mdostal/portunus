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
    OAuthRefreshTokenAuth,
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


# --- portunus-oauth-token-broker Story 01: OAuthRefreshTokenAuth ------------
# Generic across providers -- Google, GitHub, Microsoft, etc. all implement
# the same RFC 6749 §6 refresh_token grant at their own token_endpoint, so
# one class covers all of them (mirrors GCPWorkloadIdentityAuth.mint()'s
# exact shape above).

def _oauth_auth(transport, **kwargs):
    defaults = dict(
        token_endpoint="https://oauth2.example.com/token",
        client_id="client-123",
        client_secret="CLIENT-SECRET-do-not-leak",
        refresh_token="REFRESH-TOKEN-do-not-leak",
        identity="user@example.com",
        audit=AuditChain(),
        transport=transport,
    )
    defaults.update(kwargs)
    return OAuthRefreshTokenAuth(**defaults)


def test_oauth_refresh_mint_returns_access_token_and_computed_expiry():
    def transport(url, data, headers, timeout):
        return {"access_token": "ACCESS.TOKEN.VALUE", "expires_in": 3600, "scope": "read write"}

    auth = _oauth_auth(transport)
    minted = auth.mint()
    assert minted.access_token == "ACCESS.TOKEN.VALUE"
    assert minted.expires_at > 0
    assert minted.scope == "read write"
    assert minted.identity == "user@example.com"


def test_oauth_refresh_missing_access_token_raises_auth_error():
    def transport(url, data, headers, timeout):
        return {"error": "invalid_grant"}

    auth = _oauth_auth(transport)
    with pytest.raises(AuthError):
        auth.mint()


def test_oauth_refresh_mint_is_audited_without_any_credential_material(home):
    def transport(url, data, headers, timeout):
        return {"access_token": "ACCESS.TOKEN.VALUE", "expires_in": 3600}

    auth = _oauth_auth(transport)
    auth.mint()
    audit_text = (home / "audit.log").read_text()
    assert "CLIENT-SECRET-do-not-leak" not in audit_text
    assert "REFRESH-TOKEN-do-not-leak" not in audit_text
    assert "ACCESS.TOKEN.VALUE" not in audit_text
    assert "ok:oauth-refresh" in audit_text
    assert "user@example.com" in audit_text


def test_oauth_refresh_post_body_has_exactly_the_expected_fields():
    seen = {}

    def transport(url, data, headers, timeout):
        seen["url"] = url
        seen["data"] = dict(data)
        return {"access_token": "X", "expires_in": 60}

    auth = _oauth_auth(transport)
    auth.mint()
    assert seen["url"] == "https://oauth2.example.com/token"
    assert seen["data"] == {
        "grant_type": "refresh_token",
        "refresh_token": "REFRESH-TOKEN-do-not-leak",
        "client_id": "client-123",
        "client_secret": "CLIENT-SECRET-do-not-leak",
    }


def test_oauth_refresh_no_expires_in_means_unknown_expiry_not_a_crash():
    def transport(url, data, headers, timeout):
        return {"access_token": "X"}

    auth = _oauth_auth(transport)
    minted = auth.mint()
    assert minted.access_token == "X"
    assert minted.expires_at == 0


def test_long_lived_cloud_key_conformance_rejects_static_inputs(tmp_path):
    with pytest.raises(AuthError):
        assert_no_long_lived_cloud_keys(env={"AWS_ACCESS_KEY_ID": "AKIA..."})
    key_file = tmp_path / "sa.json"
    key_file.write_text('{"type": "service_account", "private_key": "SECRET"}')
    with pytest.raises(AuthError):
        assert_no_long_lived_cloud_keys(env={}, paths={"sa": key_file})
