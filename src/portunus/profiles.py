"""Account profiles: several logins per provider, one active, swapped with one command.

Mathew (2026-11-19): "make sure to get those in portunus and ensure it can swap". A person often has more than one
account with the same service -- a personal and a work gcloud login, a PostHog login per organization -- and the
tools that use them (gcloud's CLI, an MCP server) only ever hold one at a time. A profile names one of those logins;
`use` makes it the active one.

Providers:

* ``gcloud`` -- a profile is a locally credentialed gcloud account email. ``use`` runs
  ``gcloud config set account <email>``. Per-project bindings (vault-bindings.json) still pass their own
  ``--account`` and are unaffected; this is the account bare ``gcloud`` commands use.
* ``posthog`` -- a profile is a vault reference holding a PostHog personal API key. ``use`` only records which one is
  active; :func:`emit_mcp_headers` hands its value to Claude Code's MCP ``headersHelper`` so the PostHog MCP server
  authenticates as that account on its next connection.

profiles.json holds names, emails and reference names only, never a secret value.
"""
from __future__ import annotations

import json
import os
import subprocess
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Callable, Dict, Mapping, Optional, TextIO

from .atomicio import atomic_write
from .paths import home

PROVIDERS = ("gcloud", "posthog")

# What each provider's MCP headers look like. {ref} is the active profile's reference name; the resolver swaps the
# placeholder for the value at the boundary.
_HEADER_TEMPLATES: Dict[str, str] = {
    "posthog": '{{"Authorization": "Bearer {{{{secret:{ref}}}}}"}}',
}


class ProfileError(RuntimeError):
    """A profile operation could not be done. Messages carry names only, never a value."""


@dataclass
class Profile:
    name: str
    #: gcloud: the account email.
    account: str = ""
    #: posthog: the vault reference holding the API key.
    ref: str = ""
    note: str = ""


@dataclass
class ProviderProfiles:
    active: str = ""
    profiles: Dict[str, Profile] = field(default_factory=dict)


def _path(path: Optional[Path] = None) -> Path:
    return path or home() / "profiles.json"


def load_profiles(path: Optional[Path] = None) -> Dict[str, ProviderProfiles]:
    """Every provider's profiles; providers with none are present and empty."""
    p = _path(path)
    raw = json.loads(p.read_text()) if p.exists() else {}
    out: Dict[str, ProviderProfiles] = {}
    for provider in PROVIDERS:
        entry = raw.get(provider, {})
        out[provider] = ProviderProfiles(
            active=entry.get("active", ""),
            profiles={name: Profile(name=name, **{k: v for k, v in body.items() if k in ("account", "ref", "note")}) for name, body in entry.get("profiles", {}).items()},
        )
    return out


def save_profiles(data: Mapping[str, ProviderProfiles], path: Optional[Path] = None) -> None:
    body = {
        provider: {"active": pp.active, "profiles": {n: {k: v for k, v in asdict(p).items() if k != "name"} for n, p in pp.profiles.items()}}
        for provider, pp in data.items()
    }
    atomic_write(_path(path), json.dumps(body, indent=2, sort_keys=True) + "\n", mode=0o600)


def _check_provider(provider: str) -> None:
    if provider not in PROVIDERS:
        raise ProfileError(f"unknown provider {provider!r} (want one of {', '.join(PROVIDERS)})")


def add_profile(provider: str, name: str, *, account: str = "", ref: str = "", note: str = "", known_ref: Optional[Callable[[str], bool]] = None, path: Optional[Path] = None) -> Profile:
    """Add or replace a profile. The first profile for a provider becomes its active one."""
    _check_provider(provider)
    if not name:
        raise ProfileError("a profile needs a name")
    if provider == "gcloud" and not account:
        raise ProfileError("a gcloud profile needs --account <email>")
    if provider == "posthog":
        if not ref:
            raise ProfileError("a posthog profile needs --ref <vault reference holding a PostHog personal API key>")
        if known_ref is not None and not known_ref(ref):
            raise ProfileError(f"no vault reference named {ref!r}; drop the key into the vault first")
    data = load_profiles(path)
    profile = Profile(name=name, account=account, ref=ref, note=note)
    data[provider].profiles[name] = profile
    if not data[provider].active:
        data[provider].active = name
    save_profiles(data, path)
    return profile


def remove_profile(provider: str, name: str, path: Optional[Path] = None) -> None:
    _check_provider(provider)
    data = load_profiles(path)
    if name not in data[provider].profiles:
        raise ProfileError(f"no {provider} profile named {name!r}")
    del data[provider].profiles[name]
    if data[provider].active == name:
        data[provider].active = ""
    save_profiles(data, path)


def active_profile(provider: str, path: Optional[Path] = None) -> Optional[Profile]:
    _check_provider(provider)
    pp = load_profiles(path)[provider]
    return pp.profiles.get(pp.active)


def _credentialed_gcloud_accounts(runner: Callable) -> set:
    proc = runner(["gcloud", "auth", "list", "--format=json"], capture_output=True, text=True, timeout=30)
    if proc.returncode != 0:
        raise ProfileError(f"gcloud auth list failed: {(proc.stderr or '').strip()[:200]}")
    return {entry.get("account", "") for entry in json.loads(proc.stdout or "[]")}


def use_profile(provider: str, name: str, *, runner: Callable = subprocess.run, path: Optional[Path] = None) -> Profile:
    """Make `name` the active profile. For gcloud this also switches gcloud's active account."""
    _check_provider(provider)
    data = load_profiles(path)
    profile = data[provider].profiles.get(name)
    if profile is None:
        known = ", ".join(sorted(data[provider].profiles)) or "none yet"
        raise ProfileError(f"no {provider} profile named {name!r} (have: {known})")
    if provider == "gcloud":
        if profile.account not in _credentialed_gcloud_accounts(runner):
            raise ProfileError(f"{profile.account} is not logged in to gcloud; run `portunus auth login {profile.account}` first")
        proc = runner(["gcloud", "config", "set", "account", profile.account], capture_output=True, text=True, timeout=30)
        if proc.returncode != 0:
            raise ProfileError(f"gcloud config set account failed: {(proc.stderr or '').strip()[:200]}")
    data[provider].active = name
    save_profiles(data, path)
    return profile


class HeadersRefused(ProfileError):
    """mcp-headers was run somewhere other than an MCP headersHelper."""


def emit_mcp_headers(provider: str, resolver, out: TextIO, env: Mapping[str, str] = os.environ, path: Optional[Path] = None) -> None:
    """Write the active profile's MCP headers as JSON to `out` -- the stdout pipe Claude Code reads for a
    ``headersHelper``. This is a boundary sink: the value goes to that pipe and nowhere else.

    Refuses when `out` is a terminal or when Claude Code's ``CLAUDE_CODE_MCP_SERVER_NAME`` isn't set, so running it by
    hand never shows a key. The guard stops accidents, not a determined local user, who could read the vault anyway.
    """
    _check_provider(provider)
    template = _HEADER_TEMPLATES.get(provider)
    if template is None:
        raise ProfileError(f"{provider} has no MCP headers")
    if out.isatty():
        raise HeadersRefused("stdout is a terminal; mcp-headers only writes to Claude Code's headersHelper pipe")
    if not env.get("CLAUDE_CODE_MCP_SERVER_NAME"):
        raise HeadersRefused("CLAUDE_CODE_MCP_SERVER_NAME is not set; run this only as an MCP headersHelper")
    profile = active_profile(provider, path)
    if profile is None:
        raise ProfileError(f"no active {provider} profile; run `portunus profile use {provider} <name>`")

    def sink(resolved: str) -> None:
        try:
            json.loads(resolved)
        except ValueError:
            # Never echo `resolved`: it holds the value.
            raise ProfileError(f"the {provider} key in {profile.ref!r} did not make valid JSON headers") from None
        out.write(resolved)
        out.flush()

    resolver.resolve_call(template.format(ref=profile.ref), sink)
    resolver.broker.audit.append("mcp-headers", f"{provider}:{profile.name}", "ok")
