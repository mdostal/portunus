"""Mint OIDC identity tokens for a chosen audience into a file, and keep them fresh.

`portunus auth gcp` trades an OIDC token for a GCP *access* token that
Portunus itself uses. This module goes the other way: it produces a
Google-signed *identity* token (a JWT whose `aud` is whatever the caller
asks for, e.g. ``https://api.anthropic.com``) for another system to consume.
The first consumer is Anthropic Workload Identity Federation (PANT-1406,
see anthropic_federation.py), which reads the JWT from
``ANTHROPIC_IDENTITY_TOKEN_FILE`` on every exchange.

Sources (all keyless from Anthropic's side; none stores a long-lived secret):

* ``gcloud`` -- ``gcloud auth print-identity-token --audiences=A --include-email
  --impersonate-service-account=SA [--account=X]``. The usual choice on a
  non-GCP host (dragon): a gcloud login that holds
  ``roles/iam.serviceAccountTokenCreator`` on SA. A plain user credential
  cannot mint a token for a custom audience, so impersonation is required.
* ``metadata`` -- the GCE/Cloud Run/GKE metadata server, ``format=full`` so the
  token carries the ``email`` claim.
* ``wif`` -- fully keyless: the harness OIDC token goes through Google STS
  (GCPWorkloadIdentityAuth), then IAM Credentials ``generateIdToken`` for SA.

The token value is only ever written to the 0600 output file. It is never
printed, logged, or put in the audit chain; callers get a `MintedIdentityToken`
whose `token` field is excluded from repr, plus the non-secret claims
(aud/email/sub/exp/iat) decoded -- not verified -- for scheduling and display.
"""
from __future__ import annotations

import base64
import json
import os
import shutil
import subprocess
import sys
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable, Dict, List, Mapping, Optional, Sequence, TextIO

from .atomicio import atomic_write
from .audit import AuditChain
from .auth import AuthError, GCPWorkloadIdentityAuth, OIDCTokenSource

SOURCES = ("gcloud", "metadata", "wif")

METADATA_IDENTITY_URL = (
    "http://metadata.google.internal/computeMetadata/v1/instance/service-accounts/"
    "default/identity?audience={audience}&format=full"
)
GENERATE_ID_TOKEN_URL = (
    "https://iamcredentials.googleapis.com/v1/projects/-/serviceAccounts/"
    "{service_account}:generateIdToken"
)

# Refresh this long before `exp`. Google identity tokens live ~1h; Anthropic
# caps a minted access token at twice the JWT's remaining life, so a fresher
# JWT on disk means longer-lived Anthropic tokens and fewer exchanges.
DEFAULT_REFRESH_MARGIN = 900
# A JWT that carries `jti` is single-use at Anthropic: every exchange needs a
# new one on disk. Rotate at least this often when the token has one.
DEFAULT_JTI_ROTATE_SECONDS = 240
# Back-off after a failed refresh, while the old token is still on disk.
RETRY_SECONDS = 30
MIN_SLEEP = 15

Runner = Callable[..., "subprocess.CompletedProcess"]
HTTPGet = Callable[[str, Mapping[str, str], float], str]
HTTPPost = Callable[[str, Mapping[str, object], Mapping[str, str], float], Mapping[str, object]]


@dataclass(frozen=True)
class MintedIdentityToken:
    token: str = field(repr=False)
    audience: str
    email: str = ""
    subject: str = ""
    issuer: str = ""
    expires_at: int = 0
    issued_at: int = 0
    has_jti: bool = False
    source: str = ""

    def describe(self) -> str:
        """One status line, never the token."""
        parts = [f"source={self.source}", f"aud={self.audience}"]
        if self.email:
            parts.append(f"email={self.email}")
        if self.subject:
            parts.append(f"sub={self.subject}")
        parts.append(f"expires_at={self.expires_at}")
        if self.has_jti:
            parts.append("jti=yes")
        return " ".join(parts)


def decode_claims(jwt: str) -> Dict[str, object]:
    """The JWT payload, base64url-decoded WITHOUT signature verification.

    Only used for scheduling (exp/iat) and display (aud/email/sub); the
    consumer (Anthropic) verifies the signature. Raises AuthError on
    anything that isn't a three-part JWT with a JSON object payload."""
    pieces = jwt.strip().split(".")
    if len(pieces) != 3 or not pieces[1]:
        raise AuthError("identity token is not a JWT")
    payload = pieces[1] + "=" * (-len(pieces[1]) % 4)
    try:
        claims = json.loads(base64.urlsafe_b64decode(payload.encode()).decode())
    except (ValueError, UnicodeDecodeError) as exc:
        raise AuthError("identity token payload is not valid JSON") from exc
    if not isinstance(claims, dict):
        raise AuthError("identity token payload is not a JSON object")
    return claims


def _aud_str(aud: object) -> str:
    if isinstance(aud, list):
        return ",".join(str(a) for a in aud)
    return str(aud or "")


def _to_int(value: object) -> int:
    try:
        return int(value or 0)
    except (TypeError, ValueError):
        return 0


def _wrap(token: str, source: str, requested_audience: str) -> MintedIdentityToken:
    token = token.strip()
    claims = decode_claims(token)
    expires_at = _to_int(claims.get("exp"))
    if expires_at and expires_at <= int(time.time()):
        raise AuthError(f"{source}: minted identity token is already expired")
    return MintedIdentityToken(
        token=token,
        audience=_aud_str(claims.get("aud")) or requested_audience,
        email=str(claims.get("email", "") or ""),
        subject=str(claims.get("sub", "") or ""),
        issuer=str(claims.get("iss", "") or ""),
        expires_at=expires_at,
        issued_at=_to_int(claims.get("iat")),
        has_jti="jti" in claims,
        source=source,
    )


def _default_http_get(url: str, headers: Mapping[str, str], timeout: float) -> str:
    req = urllib.request.Request(url, headers=dict(headers), method="GET")
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            return resp.read().decode()
    except urllib.error.URLError as exc:
        raise AuthError(f"metadata server request failed: {type(exc).__name__}") from None


def _default_http_post(
    url: str, data: Mapping[str, object], headers: Mapping[str, str], timeout: float,
) -> Mapping[str, object]:
    req = urllib.request.Request(
        url, data=json.dumps(dict(data)).encode(), headers=dict(headers), method="POST",
    )
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            return json.loads(resp.read().decode())
    except urllib.error.HTTPError as exc:
        raise AuthError(f"request to {urllib.parse.urlparse(url).netloc} failed: HTTP {exc.code}") from None
    except (urllib.error.URLError, json.JSONDecodeError) as exc:
        raise AuthError(f"request to {urllib.parse.urlparse(url).netloc} failed: {type(exc).__name__}") from None


class IdentityTokenMinter:
    """Mint a fresh identity token for `audience` from one of SOURCES."""

    def __init__(
        self,
        audience: str,
        source: str = "gcloud",
        service_account: str = "",
        account: str = "",
        wif_audience: str = "",
        token_source: Optional[OIDCTokenSource] = None,
        audit: Optional[AuditChain] = None,
        runner: Optional[Runner] = None,
        http_get: Optional[HTTPGet] = None,
        http_post: Optional[HTTPPost] = None,
        timeout: float = 60.0,
    ):
        if source not in SOURCES:
            raise AuthError(f"unknown identity token source {source!r} (want one of {', '.join(SOURCES)})")
        if not audience:
            raise AuthError("an identity token needs an --audience")
        if source == "wif" and not (wif_audience and service_account):
            raise AuthError("source wif needs --wif-audience and --service-account")
        self.audience = audience
        self.source = source
        self.service_account = service_account
        self.account = account
        self.wif_audience = wif_audience
        self.token_source = token_source
        self.audit = audit or AuditChain()
        self.runner = runner or subprocess.run
        self.http_get = http_get or _default_http_get
        self.http_post = http_post or _default_http_post
        self.timeout = timeout

    def mint(self) -> MintedIdentityToken:
        minted = getattr(self, f"_mint_{self.source}")()
        who = minted.email or minted.subject or self.service_account or self.source
        self.audit.append("identity-token-mint", who, f"ok:{self.source}:aud={minted.audience}")
        return minted

    # --- sources -------------------------------------------------------
    def _mint_gcloud(self) -> MintedIdentityToken:
        if shutil.which("gcloud") is None and self.runner is subprocess.run:
            raise AuthError("gcloud CLI not found on PATH")
        cmd: List[str] = [
            "gcloud", "auth", "print-identity-token",
            f"--audiences={self.audience}", "--include-email",
        ]
        if self.service_account:
            cmd.append(f"--impersonate-service-account={self.service_account}")
        if self.account:
            cmd.append(f"--account={self.account}")
        try:
            proc = self.runner(cmd, capture_output=True, text=True, timeout=self.timeout)
        except subprocess.TimeoutExpired:
            raise AuthError("gcloud print-identity-token timed out") from None
        if proc.returncode != 0:
            # stderr carries gcloud's diagnosis, never the token (that is stdout).
            raise AuthError(f"gcloud print-identity-token failed: {(proc.stderr or '').strip()[:300]}")
        # Impersonation prints a WARNING line on stderr; the token is the last
        # non-empty stdout line.
        lines = [ln.strip() for ln in (proc.stdout or "").splitlines() if ln.strip()]
        if not lines:
            raise AuthError("gcloud print-identity-token returned nothing")
        return _wrap(lines[-1], "gcloud", self.audience)

    def _mint_metadata(self) -> MintedIdentityToken:
        url = METADATA_IDENTITY_URL.format(audience=urllib.parse.quote(self.audience, safe=""))
        token = self.http_get(url, {"Metadata-Flavor": "Google"}, self.timeout)
        if not token.strip():
            raise AuthError("metadata server returned an empty identity token")
        return _wrap(token, "metadata", self.audience)

    def _mint_wif(self) -> MintedIdentityToken:
        federated = GCPWorkloadIdentityAuth(
            audience=self.wif_audience, token_source=self.token_source, audit=self.audit,
        ).mint()
        url = GENERATE_ID_TOKEN_URL.format(
            service_account=urllib.parse.quote(self.service_account, safe="@.")
        )
        resp = self.http_post(
            url,
            {"audience": self.audience, "includeEmail": True},
            {"Authorization": f"Bearer {federated.access_token}", "Content-Type": "application/json"},
            self.timeout,
        )
        token = str(resp.get("token", "") or "")
        if not token:
            raise AuthError(f"generateIdToken returned no token for {self.service_account}")
        return _wrap(token, "wif", self.audience)


def write_token_file(path: Path, minted: MintedIdentityToken) -> None:
    """Atomically replace `path` with the token, 0600, parent dir 0700 when
    Portunus creates it. A reader never sees a half-written token."""
    path = Path(path).expanduser()
    if not path.parent.exists():
        path.parent.mkdir(parents=True, exist_ok=True)
        try:
            path.parent.chmod(0o700)
        except OSError:
            pass
    atomic_write(path, minted.token + "\n", mode=0o600)


def next_refresh_delay(
    minted: MintedIdentityToken,
    now: Optional[float] = None,
    margin: int = DEFAULT_REFRESH_MARGIN,
    jti_rotate_seconds: int = DEFAULT_JTI_ROTATE_SECONDS,
) -> float:
    """Seconds to wait before minting the next token.

    `exp - margin`, but never more than half the token's remaining life
    (short-lived tokens), capped at `jti_rotate_seconds` for single-use
    tokens, and never below MIN_SLEEP so a broken clock cannot spin."""
    now = time.time() if now is None else now
    if minted.expires_at <= 0:
        delay = float(margin)
    else:
        remaining = minted.expires_at - now
        delay = min(remaining - margin, remaining / 2)
    if minted.has_jti:
        delay = min(delay, float(jti_rotate_seconds))
    return max(float(MIN_SLEEP), delay)


class TokenRefresher:
    """Mint into `path` now, then keep it fresh until `stop()`.

    `run_once()` raises on failure (the caller decides whether a first mint
    failing is fatal). The background loop keeps going on failure: the old
    token stays on disk while still valid, a status line goes to `log`, and
    it retries after RETRY_SECONDS."""

    def __init__(
        self,
        minter: IdentityTokenMinter,
        path: Path,
        margin: int = DEFAULT_REFRESH_MARGIN,
        jti_rotate_seconds: int = DEFAULT_JTI_ROTATE_SECONDS,
        log: Optional[TextIO] = None,
        clock: Callable[[], float] = time.time,
    ):
        self.minter = minter
        self.path = Path(path).expanduser()
        self.margin = margin
        self.jti_rotate_seconds = jti_rotate_seconds
        self.log = log if log is not None else sys.stderr
        self.clock = clock
        self.last: Optional[MintedIdentityToken] = None
        self._stop = threading.Event()
        self._thread: Optional[threading.Thread] = None

    def _say(self, msg: str) -> None:
        try:
            print(f"portunus identity-token: {msg}", file=self.log, flush=True)
        except (OSError, ValueError):
            pass

    def run_once(self) -> MintedIdentityToken:
        minted = self.minter.mint()
        write_token_file(self.path, minted)
        self.last = minted
        return minted

    def delay(self) -> float:
        if self.last is None:
            return float(RETRY_SECONDS)
        return next_refresh_delay(self.last, self.clock(), self.margin, self.jti_rotate_seconds)

    def loop(self) -> None:
        """Refresh until stop(). Assumes run_once() already succeeded once."""
        wait = self.delay()
        while not self._stop.wait(wait):
            try:
                minted = self.run_once()
                self._say(f"refreshed {self.path} {minted.describe()}")
                wait = self.delay()
            except AuthError as exc:
                self._say(f"refresh failed, retrying in {RETRY_SECONDS}s: {exc}")
                wait = float(RETRY_SECONDS)

    def start(self) -> None:
        self._thread = threading.Thread(target=self.loop, name="portunus-identity-token", daemon=True)
        self._thread.start()

    def stop(self) -> None:
        self._stop.set()
        if self._thread is not None:
            self._thread.join(timeout=5)
