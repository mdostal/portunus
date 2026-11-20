"""Infisical adapter -- read-only, universal auth (machine identity) (GH #254).

Two ways to address a secret:

* **Full address** (self-contained, no mapping needed)::

      infisical:<project-id>:<environment>:<path/NAME>

  ``<path/NAME>`` is the folder path plus the secret key; the last segment is
  the key, everything before it the folder (``/`` when there is none). E.g.
  ``infisical:6f1c...:prod:/payments/STRIPE_KEY``.

* **Mapped** -- a plain ``sm_name`` (``STRIPE_KEY`` or ``payments/STRIPE_KEY``)
  on a reference whose Portunus *project* has an Infisical mapping (project
  id, environment, base folder, optional site URL) in
  ``PORTUNUS_HOME/infisical.json``. That is what makes ``portunus bindings set
  <project> --backend infisical`` route a whole project here.

Authentication, first match wins (all of them fail closed when absent):

1. ``INFISICAL_TOKEN`` -- an already-minted machine-identity access token.
2. ``INFISICAL_UNIVERSAL_AUTH_CLIENT_ID`` / ``INFISICAL_UNIVERSAL_AUTH_CLIENT_SECRET``
   (the Infisical CLI's own variable names).
3. Client id / secret stored in Portunus's local vault under the mapping's
   ``client_id_ref`` / ``client_secret_ref`` sm_names (defaults
   ``infisical-client-id`` / ``infisical-client-secret``) -- drop them once
   with ``portunus drop``; they never appear on argv.

The client secret is only ever sent in a POST body over HTTPS. The access token
it buys is held in memory (until shortly before it expires) and never logged,
returned, or written to disk.
"""
from __future__ import annotations

import json
import os
import time
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Callable, Dict, Optional, Tuple

from .atomicio import atomic_write
from .paths import home

SM_PREFIX = "infisical:"
SCHEME_HINT = "infisical:<project-id>:<environment>:<path/NAME>"
DEFAULT_SITE_URL = "https://app.infisical.com"
DEFAULT_CLIENT_ID_REF = "infisical-client-id"
DEFAULT_CLIENT_SECRET_REF = "infisical-client-secret"
_TOKEN_SKEW = 30  # seconds before expiry a cached token is treated as stale

# (method, url, headers, body, timeout) -> (status, body_bytes)
Http = Callable[[str, str, Dict[str, str], Optional[bytes], int], Tuple[int, bytes]]


class InfisicalError(RuntimeError):
    """Missing config/credentials, auth failure, unreachable, not found."""


@dataclass(frozen=True)
class InfisicalMapping:
    """Portunus project -> Infisical project/environment. Topology only,
    never a credential (the *_ref fields name local-vault entries)."""

    project: str
    project_id: str
    environment: str
    secret_path: str = "/"
    site_url: str = ""
    client_id_ref: str = DEFAULT_CLIENT_ID_REF
    client_secret_ref: str = DEFAULT_CLIENT_SECRET_REF


@dataclass(frozen=True)
class Address:
    project_id: str
    environment: str
    secret_path: str
    name: str
    site_url: str
    client_id_ref: str = DEFAULT_CLIENT_ID_REF
    client_secret_ref: str = DEFAULT_CLIENT_SECRET_REF


# --- mapping config --------------------------------------------------------

def config_path(path: Optional[Path] = None) -> Path:
    return Path(path) if path else home() / "infisical.json"


def load_mappings(path: Optional[Path] = None) -> Dict[str, InfisicalMapping]:
    p = config_path(path)
    if not p.exists():
        return {}
    try:
        raw = json.loads(p.read_text() or "{}")
    except json.JSONDecodeError as exc:
        raise InfisicalError(f"infisical: {p} is not valid JSON") from exc
    known = set(InfisicalMapping.__dataclass_fields__)
    out: Dict[str, InfisicalMapping] = {}
    for project, entry in (raw.get("mappings") or {}).items():
        fields = {k: v for k, v in (entry or {}).items() if k in known}
        fields["project"] = project
        try:
            out[project] = InfisicalMapping(**fields)
        except TypeError as exc:
            raise InfisicalError(f"infisical: mapping for {project!r} is incomplete ({exc})") from exc
    return out


def save_mappings(mappings: Dict[str, InfisicalMapping], path: Optional[Path] = None) -> None:
    p = config_path(path)
    p.parent.mkdir(parents=True, exist_ok=True)
    body = {"mappings": {k: {f: v for f, v in asdict(m).items() if f != "project"}
                         for k, m in sorted(mappings.items())}}
    atomic_write(p, json.dumps(body, indent=2) + "\n", mode=0o600)


# --- addressing ------------------------------------------------------------

def normalize_site_url(url: str) -> str:
    url = (url or "").strip().rstrip("/")
    if url.endswith("/api"):
        url = url[: -len("/api")]
    return url or DEFAULT_SITE_URL


def default_site_url() -> str:
    return normalize_site_url(os.environ.get("INFISICAL_SITE_URL", "") or os.environ.get("INFISICAL_API_URL", ""))


def _split_path(path_and_name: str, base: str = "/") -> Tuple[str, str]:
    rel = path_and_name.strip()
    folder, _, name = rel.rstrip("/").rpartition("/")
    if not name:
        raise InfisicalError(f"infisical: no secret name in {path_and_name!r}")
    if rel.startswith("/"):
        full = folder or "/"
    else:
        full = "/".join(p for p in (base.strip("/"), folder.strip("/")) if p)
        full = "/" + full
    return full, name


def parse_address(sm_name: str, project: str = "",
                  mappings: Optional[Dict[str, InfisicalMapping]] = None) -> Address:
    if sm_name.startswith(SM_PREFIX):
        parts = sm_name[len(SM_PREFIX):].split(":", 2)
        if len(parts) != 3 or not all(parts):
            raise InfisicalError(f"infisical backend: sm_name must be {SCHEME_HINT!r}, got {sm_name!r}")
        project_id, env, rest = parts
        folder, name = _split_path(rest)
        m = (mappings or {}).get(project)
        return Address(project_id, env, folder, name, default_site_url(),
                       client_id_ref=m.client_id_ref if m else DEFAULT_CLIENT_ID_REF,
                       client_secret_ref=m.client_secret_ref if m else DEFAULT_CLIENT_SECRET_REF)
    m = (mappings or {}).get(project)
    if m is None:
        raise InfisicalError(
            f"infisical backend: {sm_name!r} is not a full address ({SCHEME_HINT}) and project "
            f"{project!r} has no Infisical mapping -- run `portunus infisical map {project or '<project>'} "
            "--project-id <id> --env <slug>`"
        )
    folder, name = _split_path(sm_name, m.secret_path or "/")
    return Address(m.project_id, m.environment, folder, name,
                   normalize_site_url(m.site_url) if m.site_url else default_site_url(),
                   m.client_id_ref, m.client_secret_ref)


# --- HTTP -----------------------------------------------------------------

def _default_http(method, url, headers, body, timeout):  # pragma: no cover - real network
    req = urllib.request.Request(url, data=body, method=method, headers=headers)
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            return resp.status, resp.read()
    except urllib.error.HTTPError as exc:
        return exc.code, exc.read() or b""
    except (urllib.error.URLError, TimeoutError, OSError) as exc:
        reason = getattr(exc, "reason", exc)
        raise InfisicalError(f"infisical: {urllib.parse.urlsplit(url).netloc} is unreachable ({reason})") from exc


def _message(body: bytes) -> str:
    """The server's error message only -- never the raw body (which, on a
    successful read, would hold the value)."""
    try:
        data = json.loads(body or b"{}")
    except (json.JSONDecodeError, UnicodeDecodeError):
        return ""
    msg = data.get("message") if isinstance(data, dict) else ""
    return str(msg)[:200] if msg else ""


# --- client ---------------------------------------------------------------

LocalReader = Callable[[str], str]


class Client:
    def __init__(self, http: Optional[Http] = None, local_reader: Optional[LocalReader] = None,
                 timeout: int = 30, clock: Callable[[], float] = time.monotonic):
        self.http = http or _default_http
        self.local_reader = local_reader
        self.timeout = timeout
        self.clock = clock
        self._tokens: Dict[Tuple[str, str], Tuple[str, float]] = {}

    def credential_source(self, addr: Address) -> str:
        """Where credentials would come from, without reading any value."""
        if os.environ.get("INFISICAL_TOKEN"):
            return "INFISICAL_TOKEN"
        if os.environ.get("INFISICAL_UNIVERSAL_AUTH_CLIENT_ID") and os.environ.get(
                "INFISICAL_UNIVERSAL_AUTH_CLIENT_SECRET"):
            return "INFISICAL_UNIVERSAL_AUTH_CLIENT_ID/SECRET"
        return f"local vault ({addr.client_id_ref}, {addr.client_secret_ref})"

    def _client_creds(self, addr: Address) -> Tuple[str, str]:
        cid = os.environ.get("INFISICAL_UNIVERSAL_AUTH_CLIENT_ID", "")
        secret = os.environ.get("INFISICAL_UNIVERSAL_AUTH_CLIENT_SECRET", "")
        if cid and secret:
            return cid, secret
        if self.local_reader is not None:
            try:
                return self.local_reader(addr.client_id_ref), self.local_reader(addr.client_secret_ref)
            except Exception:  # noqa: BLE001 -- any local miss means "not configured"
                pass
        raise InfisicalError(
            "infisical: no machine-identity credentials -- set INFISICAL_TOKEN, or "
            "INFISICAL_UNIVERSAL_AUTH_CLIENT_ID/SECRET, or drop them into the local vault as "
            f"{addr.client_id_ref!r} / {addr.client_secret_ref!r} (`portunus drop ... --stdin`)"
        )

    def _token(self, addr: Address, refresh: bool = False) -> str:
        env_token = os.environ.get("INFISICAL_TOKEN", "")
        if env_token:
            return env_token
        cid, secret = self._client_creds(addr)
        key = (addr.site_url, cid)
        cached = self._tokens.get(key)
        if cached and not refresh and cached[1] > self.clock():
            return cached[0]
        status, body = self.http(
            "POST", f"{addr.site_url}/api/v1/auth/universal-auth/login",
            {"Content-Type": "application/json", "Accept": "application/json"},
            json.dumps({"clientId": cid, "clientSecret": secret}).encode(), self.timeout,
        )
        del secret
        if status in (401, 403):
            raise InfisicalError(f"infisical: universal-auth login was rejected ({status}) -- "
                                 "check the machine identity's client id/secret")
        if status != 200:
            raise InfisicalError(f"infisical: universal-auth login failed ({status}) {_message(body)}".rstrip())
        try:
            data = json.loads(body)
            token = data["accessToken"]
            ttl = int(data.get("expiresIn") or 0)
        except (ValueError, KeyError, TypeError) as exc:
            raise InfisicalError("infisical: malformed universal-auth login response") from exc
        self._tokens[key] = (token, self.clock() + max(ttl - _TOKEN_SKEW, 0))
        return token

    def get_secret(self, addr: Address) -> str:
        """Return ONE secret's plaintext. Only called from the resolver
        boundary (InfisicalBackend.access)."""
        query = urllib.parse.urlencode({
            "workspaceId": addr.project_id, "environment": addr.environment,
            "secretPath": addr.secret_path, "include_imports": "true",
        })
        url = f"{addr.site_url}/api/v3/secrets/raw/{urllib.parse.quote(addr.name, safe='')}?{query}"
        for attempt in (0, 1):
            headers = {"Authorization": f"Bearer {self._token(addr, refresh=attempt == 1)}",
                       "Accept": "application/json"}
            status, body = self.http("GET", url, headers, None, self.timeout)
            if status == 401 and attempt == 0 and not os.environ.get("INFISICAL_TOKEN"):
                continue  # cached token expired early: re-login once, same store
            break
        where = f"{addr.secret_path.rstrip('/')}/{addr.name} in {addr.environment}"
        if status == 404:
            raise InfisicalError(f"infisical: NOT_FOUND: no secret {where} (project {addr.project_id})")
        if status in (401, 403):
            raise InfisicalError(f"infisical: access to {where} denied ({status}) -- the machine "
                                 "identity needs read access to this project/environment")
        if status != 200:
            raise InfisicalError(f"infisical: reading {where} failed ({status}) {_message(body)}".rstrip())
        try:
            return json.loads(body)["secret"]["secretValue"]
        except (ValueError, KeyError, TypeError) as exc:
            raise InfisicalError("infisical: malformed secret response") from exc
