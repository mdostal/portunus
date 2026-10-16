"""Credential liveness -- is a stored credential still accepted by its
provider? Detection only, never rotation (rotation.py owns that).

One shared primitive, per-provider checks registered in `_CHECKS`, so the
sibling liveness issues (Autumn #143, Vercel #144, Clerk #145) each add one
function here instead of a new runner. GitHub is the first check
(PANT-1141 / portunus#188).

Every check receives the plaintext ONLY inside a `Resolver.resolve_call()`
boundary and returns a `LivenessResult` -- a status word and a short
non-secret detail. A provider response body is never logged or returned.

There is no scheduler in Portunus yet (#141 tracks that); `portunus
rotation liveness` is the entry point a cron/systemd timer/CI schedule
calls, and it exits 2 when anything is dead so the schedule fails loudly.
"""
from __future__ import annotations

import time
from dataclasses import asdict, dataclass, field
from typing import Callable, Dict, List

from .rotation import (
    GITHUB_API,
    _github_headers,
    _github_http,
    github_token_subtype,
)

LIVE = "live"
DEAD = "dead"
UNKNOWN = "unknown"          # check ran but the answer was ambiguous (network, 5xx, 403 ...)
UNSUPPORTED = "unsupported"  # no check exists for this provider / credential type


@dataclass(frozen=True)
class LivenessResult:
    """Outcome of one liveness check. Structurally incapable of carrying
    credential material -- names, a status word and a short detail only."""

    ref_name: str
    provider: str
    status: str
    detail: str = ""
    timestamp: int = field(default_factory=lambda: int(time.time()))

    def to_dict(self) -> dict:
        return asdict(self)


def check_github(ref, resolver, http=None, timeout: float = 15.0) -> LivenessResult:
    """`GET /rate_limit` with the stored token: 200 = live, 401 = dead.
    /rate_limit doesn't count against the REST rate limit and accepts every
    bearer-token type (PAT, OAuth, App user, installation), so one call
    covers them all. App private keys can't be checked this way (they sign
    a JWT, they aren't bearer tokens) -> unsupported."""
    http = http or _github_http
    tags = getattr(ref, "tags", None) or {}
    if github_token_subtype(tags=tags) == "app_private_key":
        return LivenessResult(ref.name, "github", UNSUPPORTED, "GitHub App private key -- not a bearer token")

    def _probe(value: str) -> LivenessResult:
        subtype = github_token_subtype(value=value, tags=tags)
        if subtype == "app_private_key":
            return LivenessResult(ref.name, "github", UNSUPPORTED, "GitHub App private key -- not a bearer token")
        try:
            status, _ = http(
                "GET",
                f"{GITHUB_API}/rate_limit",
                _github_headers({"Authorization": f"Bearer {value.strip()}"}),
                None,
                timeout,
            )
        except Exception as exc:  # network error: say so, never guess
            return LivenessResult(ref.name, "github", UNKNOWN, f"request failed: {type(exc).__name__}")
        if status == 200:
            return LivenessResult(ref.name, "github", LIVE, "GET /rate_limit -> 200")
        if status == 401:
            detail = "GET /rate_limit -> 401 Bad credentials"
            if subtype == "app_installation":
                detail += " (installation tokens expire after 1h -- expected for a stored ghs_)"
            return LivenessResult(ref.name, "github", DEAD, detail)
        return LivenessResult(ref.name, "github", UNKNOWN, f"GET /rate_limit -> {status}")

    return resolver.resolve_call("{{secret:" + ref.name + "}}", _probe)


_CHECKS: Dict[str, Callable[..., LivenessResult]] = {
    "github": check_github,
}


def _provider_of(ref) -> str:
    sm_name = getattr(ref, "sm_name", "") or ""
    return ref.provider or (sm_name.split(":")[0] if ":" in sm_name else "")


def check_reference(ref, resolver, http=None) -> LivenessResult:
    """Run the registered check for `ref`'s provider. A ref that isn't
    holding a value yet (state != enabled, e.g. a `requested` placeholder)
    or whose resolution fails is reported, not raised."""
    provider = _provider_of(ref)
    check = _CHECKS.get(provider)
    if check is None:
        return LivenessResult(ref.name, provider, UNSUPPORTED, "no liveness check for this provider yet")
    if getattr(ref, "state", "enabled") != "enabled":
        return LivenessResult(ref.name, provider, UNSUPPORTED, f"reference state is {ref.state!r} -- no value to check")
    try:
        return check(ref, resolver, http=http) if http is not None else check(ref, resolver)
    except Exception as exc:
        return LivenessResult(ref.name, provider, UNKNOWN, f"could not resolve: {type(exc).__name__}")


def run_liveness(
    registry,
    resolver,
    provider: str = "",
    ref_name: str = "",
    http=None,
    audit=None,
) -> List[LivenessResult]:
    """Check every reference with a registered liveness check (optionally
    narrowed to one provider or one reference). Each result is appended to
    the audit chain as `liveness <sm_name> ok:live|err:dead|warn:<status>`
    so a dead credential leaves a persistent trail, not just a log line."""
    results: List[LivenessResult] = []
    for ref in registry:
        if ref_name and ref.name != ref_name:
            continue
        prov = _provider_of(ref)
        if provider and prov != provider:
            continue
        if not ref_name and prov not in _CHECKS:
            continue
        result = check_reference(ref, resolver, http=http)
        results.append(result)
        if audit is not None and result.status != UNSUPPORTED:
            tag = {LIVE: "ok:live", DEAD: "err:dead"}.get(result.status, f"warn:{result.status}")
            audit.append("liveness", ref.sm_name, tag)
    return results


def supported_providers() -> List[str]:
    return sorted(_CHECKS)
