"""Anthropic Workload Identity Federation profiles (PANT-1406).

A Claude workload (Claude Code, an Anthropic SDK) can authenticate with a
short-lived OIDC identity token instead of a stored ``ANTHROPIC_API_KEY``:
it reads a JWT from ``ANTHROPIC_IDENTITY_TOKEN_FILE``, exchanges it at
``POST https://api.anthropic.com/v1/oauth/token`` under a federation rule,
and gets back a short-lived access token. Portunus supplies that JWT
(identity_token.py) and the five env vars that point the workload at it.

One profile per Anthropic org/account (e.g. ``mathew`` and ``ff-events``),
each with its own federation rule and Anthropic service account. A lane
picks its account by running its agent under
``portunus anthropic-federation exec <name> -- <cmd>``.

Precedence is the trap: in Claude Code ``ANTHROPIC_AUTH_TOKEN``,
``ANTHROPIC_API_KEY``, ``apiKeyHelper`` and ``CLAUDE_CODE_OAUTH_TOKEN`` all
outrank federation, and the SDK ranks ``ANTHROPIC_PROFILE`` above the env
federation vars. A federated child that inherits any of them silently bills
the wrong account. :func:`build_env` therefore removes them (an
empty-string value still occupies its precedence slot, so they are unset,
not blanked), and :func:`conflicts` reports any that are present.

anthropic-federation.json holds IDs and paths only -- no secret. The IDs
(org UUID, ``fdrl_``/``svac_``/``wrkspc_``) identify, they do not authenticate.
"""
from __future__ import annotations

import json
import os
import re
import signal
import subprocess
import sys
import urllib.error
import urllib.request
from dataclasses import asdict, dataclass, fields
from pathlib import Path
from typing import Callable, Dict, List, Mapping, Optional, Sequence, TextIO

from .atomicio import atomic_write
from .auth import AuthError
from .filelock import flock_path
from .identity_token import (
    DEFAULT_REFRESH_MARGIN,
    SOURCES,
    IdentityTokenMinter,
    TokenRefresher,
)
from .paths import home

DEFAULT_AUDIENCE = "https://api.anthropic.com"
TOKEN_ENDPOINT = "https://api.anthropic.com/v1/oauth/token"
JWT_BEARER = "urn:ietf:params:oauth:grant-type:jwt-bearer"

#: Credentials that outrank the federation env vars in Claude Code or the
#: Anthropic SDK. A federated child must not inherit any of these.
CONFLICTING_ENV = (
    "ANTHROPIC_API_KEY",
    "ANTHROPIC_AUTH_TOKEN",
    "CLAUDE_CODE_OAUTH_TOKEN",
    "ANTHROPIC_PROFILE",
    "ANTHROPIC_IDENTITY_TOKEN",
    "CLAUDE_CODE_USE_BEDROCK",
    "CLAUDE_CODE_USE_VERTEX",
    "CLAUDE_CODE_USE_FOUNDRY",
)
FEDERATION_ENV = (
    "ANTHROPIC_FEDERATION_RULE_ID",
    "ANTHROPIC_ORGANIZATION_ID",
    "ANTHROPIC_SERVICE_ACCOUNT_ID",
    "ANTHROPIC_WORKSPACE_ID",
    "ANTHROPIC_IDENTITY_TOKEN_FILE",
)

_UUID = re.compile(r"^[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}$")
_NAME = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,63}$")


class FederationError(RuntimeError):
    """A federation profile operation failed. Messages carry names and IDs only."""


@dataclass
class FederationProfile:
    name: str
    organization_id: str
    federation_rule_id: str
    service_account_id: str = ""
    workspace_id: str = ""
    #: Where the JWT lives; default home()/anthropic-federation/<name>.jwt.
    token_file: str = ""
    audience: str = DEFAULT_AUDIENCE
    #: identity_token source: gcloud | metadata | wif.
    source: str = "gcloud"
    #: GCP service account the identity token is minted for (gcloud
    #: impersonation target, or the wif generateIdToken target).
    impersonate_service_account: str = ""
    #: gcloud only: which local gcloud login does the impersonating.
    gcloud_account: str = ""
    #: wif only: the GCP workload identity provider audience for STS.
    wif_audience: str = ""
    refresh_margin: int = DEFAULT_REFRESH_MARGIN
    note: str = ""

    def resolved_token_file(self) -> Path:
        if self.token_file:
            return Path(self.token_file).expanduser()
        return home() / "anthropic-federation" / f"{self.name}.jwt"

    def minter(self, **kw) -> IdentityTokenMinter:
        return IdentityTokenMinter(
            audience=self.audience,
            source=self.source,
            service_account=self.impersonate_service_account,
            account=self.gcloud_account,
            wif_audience=self.wif_audience,
            **kw,
        )


def validate(profile: FederationProfile) -> None:
    if not _NAME.match(profile.name):
        raise FederationError(f"invalid profile name {profile.name!r} (letters, digits, . _ -)")
    if not _UUID.match(profile.organization_id):
        raise FederationError("organization_id must be the Anthropic organization UUID")
    if not profile.federation_rule_id.startswith("fdrl_"):
        raise FederationError("federation_rule_id must start with fdrl_")
    if profile.service_account_id and not profile.service_account_id.startswith("svac_"):
        raise FederationError("service_account_id must start with svac_")
    if profile.workspace_id and not profile.workspace_id.startswith("wrkspc_"):
        raise FederationError("workspace_id must start with wrkspc_")
    if profile.source not in SOURCES:
        raise FederationError(f"source must be one of {', '.join(SOURCES)}")
    if profile.source == "wif" and not (profile.wif_audience and profile.impersonate_service_account):
        raise FederationError("source wif needs wif_audience and impersonate_service_account")
    if not profile.audience:
        raise FederationError("audience must not be empty")
    if profile.refresh_margin < 60:
        raise FederationError("refresh_margin must be at least 60 seconds")


# --- store -------------------------------------------------------------
def _path(path: Optional[Path] = None) -> Path:
    return path or home() / "anthropic-federation.json"


_FIELDS = {f.name for f in fields(FederationProfile)} - {"name"}


def load(path: Optional[Path] = None) -> Dict[str, FederationProfile]:
    p = _path(path)
    if not p.exists():
        return {}
    try:
        raw = json.loads(p.read_text())
    except json.JSONDecodeError as exc:
        raise FederationError(f"{p.name} is not valid JSON") from exc
    if not isinstance(raw, dict):
        raise FederationError(f"{p.name} must be a JSON object")
    profiles = raw.get("profiles")
    if profiles is None:
        profiles = {}
    if not isinstance(profiles, dict):
        raise FederationError(f"{p.name}: 'profiles' must be an object")
    out: Dict[str, FederationProfile] = {}
    for name, body in profiles.items():
        if not isinstance(body, dict):
            raise FederationError(f"{p.name}: profile {name!r} must be an object")
        out[name] = FederationProfile(name=name, **{k: v for k, v in body.items() if k in _FIELDS})
    return out


def _save(profiles: Mapping[str, FederationProfile], path: Optional[Path] = None) -> None:
    body = {"profiles": {n: {k: v for k, v in asdict(p).items() if k != "name"} for n, p in sorted(profiles.items())}}
    atomic_write(_path(path), json.dumps(body, indent=2) + "\n", mode=0o600)


def get(name: str, path: Optional[Path] = None) -> FederationProfile:
    profiles = load(path)
    if name not in profiles:
        known = ", ".join(sorted(profiles)) or "none"
        raise FederationError(f"no anthropic federation profile {name!r} (known: {known})")
    return profiles[name]


def upsert(name: str, path: Optional[Path] = None, **changes) -> FederationProfile:
    """Create `name`, or update only the fields given (None = leave as is)."""
    p = _path(path)
    with flock_path(p.with_name(p.name + ".lock")):
        profiles = load(p)
        changes = {k: v for k, v in changes.items() if v is not None}
        unknown = set(changes) - _FIELDS
        if unknown:
            raise FederationError(f"unknown field(s): {', '.join(sorted(unknown))}")
        if name in profiles:
            merged = {**asdict(profiles[name]), **changes}
        else:
            missing = [k for k in ("organization_id", "federation_rule_id") if not changes.get(k)]
            if missing:
                raise FederationError(f"new profile {name!r} needs {', '.join(missing)}")
            merged = {"name": name, **changes}
        profile = FederationProfile(**merged)
        validate(profile)
        profiles[name] = profile
        _save(profiles, p)
        return profile


def remove(name: str, path: Optional[Path] = None) -> None:
    p = _path(path)
    with flock_path(p.with_name(p.name + ".lock")):
        profiles = load(p)
        if name not in profiles:
            raise FederationError(f"no anthropic federation profile {name!r}")
        del profiles[name]
        _save(profiles, p)


# --- environment -------------------------------------------------------
def federation_vars(profile: FederationProfile) -> Dict[str, str]:
    """The ANTHROPIC_* vars for `profile`; optional ones only when set."""
    out = {
        "ANTHROPIC_FEDERATION_RULE_ID": profile.federation_rule_id,
        "ANTHROPIC_ORGANIZATION_ID": profile.organization_id,
        "ANTHROPIC_IDENTITY_TOKEN_FILE": str(profile.resolved_token_file()),
    }
    if profile.service_account_id:
        out["ANTHROPIC_SERVICE_ACCOUNT_ID"] = profile.service_account_id
    if profile.workspace_id:
        out["ANTHROPIC_WORKSPACE_ID"] = profile.workspace_id
    return out


def unset_vars(profile: FederationProfile) -> List[str]:
    """Names a federated child must NOT have: every outranking credential,
    plus the optional federation vars this profile leaves empty (so a value
    inherited from another profile can't leak in)."""
    mine = federation_vars(profile)
    return list(CONFLICTING_ENV) + [k for k in FEDERATION_ENV if k not in mine]


def build_env(profile: FederationProfile, base: Optional[Mapping[str, str]] = None) -> Dict[str, str]:
    env = dict(os.environ if base is None else base)
    for name in unset_vars(profile):
        env.pop(name, None)
    env.update(federation_vars(profile))
    return env


def conflicts(env: Optional[Mapping[str, str]] = None) -> List[str]:
    """Names (never values) of outranking credentials present in `env`,
    including empty ones -- an empty var still takes its precedence slot."""
    env = os.environ if env is None else env
    return [name for name in CONFLICTING_ENV if name in env]


def claude_settings_conflicts(paths: Optional[Sequence[Path]] = None) -> List[str]:
    """Claude Code settings files that set `apiKeyHelper` (it outranks
    federation) or an outranking credential under `env`. Paths only."""
    if paths is None:
        cfg = Path(os.environ.get("CLAUDE_CONFIG_DIR") or Path.home() / ".claude")
        paths = [cfg / "settings.json", cfg / "settings.local.json",
                 Path.cwd() / ".claude" / "settings.json", Path.cwd() / ".claude" / "settings.local.json"]
    found: List[str] = []
    for p in paths:
        try:
            data = json.loads(Path(p).read_text())
        except (OSError, json.JSONDecodeError):
            continue
        if not isinstance(data, dict):
            continue
        if data.get("apiKeyHelper"):
            found.append(f"{p}: apiKeyHelper")
        env = data.get("env") if isinstance(data.get("env"), dict) else {}
        for name in CONFLICTING_ENV:
            if name in env:
                found.append(f"{p}: env.{name}")
    return found


def shell_lines(profile: FederationProfile) -> List[str]:
    """POSIX-shell lines that switch the current shell to `profile`."""
    import shlex
    lines = [f"unset {name}" for name in unset_vars(profile)]
    lines += [f"export {k}={shlex.quote(v)}" for k, v in federation_vars(profile).items()]
    return lines


# --- live exchange (verify) -------------------------------------------
Transport = Callable[[str, Mapping[str, object], float], Mapping[str, object]]


def _default_transport(url: str, body: Mapping[str, object], timeout: float) -> Mapping[str, object]:
    req = urllib.request.Request(
        url, data=json.dumps(dict(body)).encode(),
        headers={"Content-Type": "application/json"}, method="POST",
    )
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            return json.loads(resp.read().decode())
    except urllib.error.HTTPError as exc:
        hint = " (see Console > Workload identity > authentication history for the reason)" if exc.code in (400, 401, 403) else ""
        raise FederationError(f"token exchange failed: HTTP {exc.code}{hint}") from None
    except (urllib.error.URLError, json.JSONDecodeError) as exc:
        raise FederationError(f"token exchange failed: {type(exc).__name__}") from None


def exchange(profile: FederationProfile, jwt: str, transport: Optional[Transport] = None,
             timeout: float = 30.0) -> Dict[str, object]:
    """Trade `jwt` for an Anthropic access token and return only the
    non-secret parts: token_type, expires_in, scope. The access token is
    dropped here and never reaches the caller."""
    body: Dict[str, object] = {
        "grant_type": JWT_BEARER,
        "assertion": jwt,
        "federation_rule_id": profile.federation_rule_id,
        "organization_id": profile.organization_id,
    }
    if profile.service_account_id:
        body["service_account_id"] = profile.service_account_id
    if profile.workspace_id:
        body["workspace_id"] = profile.workspace_id
    resp = (transport or _default_transport)(TOKEN_ENDPOINT, body, timeout)
    if not resp.get("access_token"):
        raise FederationError("token exchange returned no access_token")
    return {k: resp.get(k) for k in ("token_type", "expires_in", "scope") if k in resp}


# --- exec ---------------------------------------------------------------
def run_exec(
    profile: FederationProfile,
    cmd: Sequence[str],
    minter: Optional[IdentityTokenMinter] = None,
    log: Optional[TextIO] = None,
    popen: Callable[..., "subprocess.Popen"] = subprocess.Popen,
) -> int:
    """Mint the JWT, keep it fresh in a background thread for as long as
    the child runs, and run `cmd` with the federated env. Returns the
    child's exit code (128+N if it died on signal N)."""
    if not cmd:
        raise FederationError("exec needs a command after --")
    log = log if log is not None else sys.stderr
    refresher = TokenRefresher(minter or profile.minter(), profile.resolved_token_file(),
                               margin=profile.refresh_margin, log=log)
    try:
        refresher.run_once()
    except AuthError as exc:
        raise FederationError(f"could not mint the identity token for {profile.name!r}: {exc}") from None
    refresher.start()
    try:
        child = popen(list(cmd), env=build_env(profile))
    except OSError as exc:
        refresher.stop()
        raise FederationError(f"could not start {cmd[0]!r}: {exc.strerror or exc}") from None

    forwarded = (signal.SIGINT, signal.SIGTERM, signal.SIGHUP)
    previous = {}

    def _forward(signum, _frame):
        try:
            child.send_signal(signum)
        except (OSError, ProcessLookupError):
            pass

    try:
        for s in forwarded:
            try:
                previous[s] = signal.signal(s, _forward)
            except (ValueError, OSError):  # not main thread / unsupported
                pass
        code = child.wait()
    finally:
        for s, h in previous.items():
            signal.signal(s, h)
        refresher.stop()
    return 128 - code if code < 0 else code
