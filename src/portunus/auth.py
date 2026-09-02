"""Harness-side keyless cloud authentication.

This module exchanges a short-lived agent OIDC token for short-lived cloud
credentials. Token material is accepted only from harness-controlled sources
and is never written to Portunus state or audit logs.
"""
from __future__ import annotations

import json
import os
import time
import urllib.error
import urllib.parse
import urllib.request
import xml.etree.ElementTree as ET
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable, Dict, Mapping, Optional, Protocol

from .audit import AuditChain


class AuthError(RuntimeError):
    """Raised when federated credentials cannot be minted."""


@dataclass(frozen=True)
class OIDCToken:
    token: str = field(repr=False)
    issuer: str
    subject: str
    audience: str
    expires_at: int = 0

    def expired(self, now: Optional[int] = None, skew: int = 30) -> bool:
        if self.expires_at <= 0:
            return False
        return self.expires_at <= int(now if now is not None else time.time()) + skew


class OIDCTokenSource(Protocol):
    def get(self) -> OIDCToken:
        """Return a fresh short-lived OIDC token."""
        ...


class EnvOIDCTokenSource:
    """Read the harness-provided OIDC token from env or a 0600-ish file.

    The token value is intentionally not parsed or logged by Portunus. The
    issuer, subject, and audience are explicit non-secret metadata supplied by
    the harness for audit and cloud trust-policy matching.
    """

    def __init__(
        self,
        token_env: str = "PORTUNUS_OIDC_TOKEN",
        token_file_env: str = "PORTUNUS_OIDC_TOKEN_FILE",
    ):
        self.token_env = token_env
        self.token_file_env = token_file_env

    def get(self) -> OIDCToken:
        token = os.environ.get(self.token_env, "")
        token_file = os.environ.get(self.token_file_env, "")
        if token_file:
            try:
                token = Path(token_file).read_text().strip()
            except OSError as exc:
                raise AuthError("OIDC token file is not readable") from exc
        if not token:
            raise AuthError("OIDC token not provided by harness")

        expires_at = 0
        raw_exp = os.environ.get("PORTUNUS_OIDC_EXPIRES_AT", "")
        if raw_exp:
            try:
                expires_at = int(raw_exp)
            except ValueError as exc:
                raise AuthError("PORTUNUS_OIDC_EXPIRES_AT must be an epoch integer") from exc

        oidc = OIDCToken(
            token=token,
            issuer=os.environ.get("PORTUNUS_OIDC_ISSUER", ""),
            subject=os.environ.get("PORTUNUS_OIDC_SUBJECT", ""),
            audience=os.environ.get("PORTUNUS_OIDC_AUDIENCE", ""),
            expires_at=expires_at,
        )
        if oidc.expired():
            raise AuthError("OIDC token is expired or too close to expiry")
        return oidc


@dataclass(frozen=True)
class GCPAccessToken:
    access_token: str = field(repr=False)
    expires_at: int
    identity: str
    scope: str


@dataclass(frozen=True)
class AWSSessionCredentials:
    access_key_id: str = field(repr=False)
    secret_access_key: str = field(repr=False)
    session_token: str = field(repr=False)
    expires_at: int
    identity: str


GCPTransport = Callable[[str, Mapping[str, str], Mapping[str, str], float], Mapping[str, object]]
AWSTransport = Callable[[str, Mapping[str, str], float], str]


def _audit_identity(oidc: OIDCToken, fallback: str = "unknown") -> str:
    return oidc.subject or oidc.issuer or fallback


def _default_gcp_transport(
    url: str, data: Mapping[str, str], headers: Mapping[str, str], timeout: float
) -> Mapping[str, object]:
    body = urllib.parse.urlencode(data).encode()
    req = urllib.request.Request(url, data=body, headers=dict(headers), method="POST")
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            return json.loads(resp.read().decode())
    except (urllib.error.URLError, json.JSONDecodeError) as exc:
        raise AuthError("GCP token exchange failed") from exc


def _default_aws_transport(url: str, data: Mapping[str, str], timeout: float) -> str:
    body = urllib.parse.urlencode(data).encode()
    req = urllib.request.Request(
        url,
        data=body,
        headers={"Content-Type": "application/x-www-form-urlencoded"},
        method="POST",
    )
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            return resp.read().decode()
    except urllib.error.URLError as exc:
        raise AuthError("AWS web identity exchange failed") from exc


class GCPWorkloadIdentityAuth:
    """Exchange OIDC for a scoped GCP access token via Security Token Service."""

    token_url = "https://sts.googleapis.com/v1/token"

    def __init__(
        self,
        audience: str,
        token_source: Optional[OIDCTokenSource] = None,
        scope: str = "https://www.googleapis.com/auth/cloud-platform",
        audit: Optional[AuditChain] = None,
        transport: Optional[GCPTransport] = None,
        timeout: float = 30.0,
    ):
        self.audience = audience
        self.token_source = token_source or EnvOIDCTokenSource()
        self.scope = scope
        self.audit = audit or AuditChain()
        self.transport = transport or _default_gcp_transport
        self.timeout = timeout

    def mint(self) -> GCPAccessToken:
        oidc = self.token_source.get()
        if not self.audience:
            raise AuthError("GCP workload identity audience is required")
        data = {
            "grant_type": "urn:ietf:params:oauth:grant-type:token-exchange",
            "audience": self.audience,
            "scope": self.scope,
            "requested_token_type": "urn:ietf:params:oauth:token-type:access_token",
            "subject_token_type": "urn:ietf:params:oauth:token-type:jwt",
            "subject_token": oidc.token,
        }
        resp = self.transport(
            self.token_url,
            data,
            {"Content-Type": "application/x-www-form-urlencoded"},
            self.timeout,
        )
        access_token = str(resp.get("access_token", ""))
        if not access_token:
            raise AuthError("GCP token exchange did not return an access token")
        expires_in = int(resp.get("expires_in", 0) or 0)
        expires_at = int(time.time()) + expires_in if expires_in else 0
        identity = _audit_identity(oidc)
        self.audit.append("credential-mint", identity, "ok:gcp-wif")
        return GCPAccessToken(
            access_token=access_token,
            expires_at=expires_at,
            identity=identity,
            scope=self.scope,
        )


class AWSWebIdentityAuth:
    """Exchange OIDC for short-lived AWS STS credentials."""

    token_url = "https://sts.amazonaws.com/"

    def __init__(
        self,
        role_arn: str,
        token_source: Optional[OIDCTokenSource] = None,
        session_name: str = "portunus-agent",
        duration_seconds: int = 900,
        audit: Optional[AuditChain] = None,
        transport: Optional[AWSTransport] = None,
        timeout: float = 30.0,
    ):
        self.role_arn = role_arn
        self.token_source = token_source or EnvOIDCTokenSource()
        self.session_name = session_name
        self.duration_seconds = duration_seconds
        self.audit = audit or AuditChain()
        self.transport = transport or _default_aws_transport
        self.timeout = timeout

    def mint(self) -> AWSSessionCredentials:
        oidc = self.token_source.get()
        if not self.role_arn:
            raise AuthError("AWS role ARN is required")
        data = {
            "Action": "AssumeRoleWithWebIdentity",
            "Version": "2011-06-15",
            "RoleArn": self.role_arn,
            "RoleSessionName": self.session_name,
            "DurationSeconds": str(self.duration_seconds),
            "WebIdentityToken": oidc.token,
        }
        xml_text = self.transport(self.token_url, data, self.timeout)
        try:
            root = ET.fromstring(xml_text)
        except ET.ParseError as exc:
            raise AuthError("AWS STS returned invalid XML") from exc

        def text(name: str) -> str:
            node = root.find(f".//{{*}}{name}")
            return node.text if node is not None and node.text is not None else ""

        access_key_id = text("AccessKeyId")
        secret_access_key = text("SecretAccessKey")
        session_token = text("SessionToken")
        if not access_key_id or not secret_access_key or not session_token:
            raise AuthError("AWS STS response did not include session credentials")
        identity = _audit_identity(oidc)
        self.audit.append("credential-mint", identity, "ok:aws-web-identity")
        return AWSSessionCredentials(
            access_key_id=access_key_id,
            secret_access_key=secret_access_key,
            session_token=session_token,
            expires_at=0,
            identity=identity,
        )


@dataclass(frozen=True)
class OAuthAccessToken:
    access_token: str = field(repr=False)
    expires_at: int
    identity: str
    scope: str = ""


class OAuthRefreshTokenAuth:
    """Exchange a stored OAuth refresh token for a short-lived access token
    via the standard OAuth 2.0 refresh_token grant (RFC 6749 SS6). Generic
    across providers -- Google, GitHub, Microsoft, etc. all implement this
    same grant at their own token_endpoint, so one class covers all of
    them; mirrors GCPWorkloadIdentityAuth.mint()'s exact shape above.

    Portunus never runs the initial OAuth consent flow itself (see
    portunus-oauth-token-broker/docs/design-discussion.md) -- client_id/
    client_secret/refresh_token are supplied from a credential bundle the
    user bootstrapped through a provider-legitimate mechanism (e.g.
    `gcloud auth application-default login --scopes=...`) and stored via
    `portunus oauth store`. This class only ever mints -- it never touches
    the consent/redirect flow.
    """

    def __init__(
        self,
        token_endpoint: str,
        client_id: str,
        client_secret: str,
        refresh_token: str,
        identity: str = "",
        audit: Optional[AuditChain] = None,
        transport: Optional[GCPTransport] = None,
        timeout: float = 30.0,
    ):
        self.token_endpoint = token_endpoint
        self.client_id = client_id
        self.client_secret = client_secret
        self.refresh_token = refresh_token
        self.identity = identity
        self.audit = audit or AuditChain()
        self.transport = transport or _default_gcp_transport
        self.timeout = timeout

    def mint(self) -> OAuthAccessToken:
        data = {
            "grant_type": "refresh_token",
            "refresh_token": self.refresh_token,
            "client_id": self.client_id,
            "client_secret": self.client_secret,
        }
        resp = self.transport(
            self.token_endpoint,
            data,
            {"Content-Type": "application/x-www-form-urlencoded"},
            self.timeout,
        )
        access_token = str(resp.get("access_token", ""))
        if not access_token:
            raise AuthError("OAuth refresh grant did not return an access token")
        expires_in = int(resp.get("expires_in", 0) or 0)
        expires_at = int(time.time()) + expires_in if expires_in else 0
        scope = str(resp.get("scope", ""))
        self.audit.append("credential-mint", self.identity or "oauth", "ok:oauth-refresh")
        return OAuthAccessToken(
            access_token=access_token,
            expires_at=expires_at,
            identity=self.identity,
            scope=scope,
        )


def assert_no_long_lived_cloud_keys(
    env: Optional[Mapping[str, str]] = None,
    paths: Optional[Mapping[str, Path]] = None,
) -> None:
    """Fail if common long-lived cloud key material is present.

    This conformance helper is deliberately conservative: Portunus accepts
    short-lived OIDC tokens and federated credentials, but rejects static GCP
    service-account JSON and AWS access-key pairs in harness-visible inputs.
    """
    env = env if env is not None else os.environ
    banned_env = (
        "GOOGLE_APPLICATION_CREDENTIALS",
        "AWS_ACCESS_KEY_ID",
        "AWS_SECRET_ACCESS_KEY",
    )
    present = [name for name in banned_env if env.get(name)]
    if present:
        raise AuthError("long-lived cloud credential env is present: " + ", ".join(present))

    for label, path in (paths or {}).items():
        try:
            text = Path(path).read_text()
        except OSError:
            continue
        if '"type": "service_account"' in text or "private_key" in text:
            raise AuthError(f"long-lived GCP service account key found in {label}")
