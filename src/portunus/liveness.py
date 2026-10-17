"""Credential liveness + cross-system parity -- the shared primitives every
provider adapter (Vercel, Autumn, Clerk, Convex; GH #141) builds on.

Rotation (rotation.py) answers "can Portunus mint a new one?". Liveness
answers the question that actually went unanswered for months in the
2026-09-13 incident: "does the stored copy still work at all?" -- a dead
Vercel deploy token and an invalid Autumn key sat in GCP Secret Manager
undetected because nothing ever exercised them.

Liveness:
- A `LivenessAdapter` makes one cheap, authenticated, read-only call
  against the provider's own API and classifies the HTTP status as
  live / dead / unknown. `HTTPLivenessAdapter` covers every provider whose
  check is "send the key as a bearer token, look at the status code";
  a provider only has to supply an `HTTPProbe`.
- The value is resolved through `Resolver.resolve_call()`, so it only ever
  reaches the outbound request header (the boundary) -- same
  boundary-only invariant as every other sink. The default transport
  returns the status code ONLY; the response body is never read into a
  result, a log line, or the audit log.
- Results persist to PORTUNUS_HOME/liveness-status.json (0600, locked,
  atomic-replace). A failing reference keeps its `first_failed_at` until a
  live check clears it, so a dead key is a standing, ageing finding -- the
  same escalation-clock idea as leak-status.json -- not a log line that
  scrolls away.

Parity:
- `parity_check()` compares one reference's value across several targets
  (GCP Secret Manager, the local vault, a Convex deployment, ...). Each
  target is a zero-arg fetcher. Values are compared via HMAC under a fresh
  random per-call key and dropped immediately, so the result carries only
  target names grouped by equality -- never a value, and never a
  fingerprint that could be compared across runs.

Built-in provider probes are registered at the bottom of this module by the
provider sub-issues (GitHub, PANT-1141, is the first; the event-api sources
Eventbrite, Ticketmaster, SeatGeek and Meetup, PANT-1126, follow). See
docs/liveness.md.
"""
from __future__ import annotations

import hashlib
import hmac
import json
import secrets
import time
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Callable, Dict, Iterable, List, Mapping, Optional, Tuple

from .atomicio import atomic_write
from .backend import BackendError
from .broker import ApprovalRequired, NotAuthorized, NotInjectable
from .filelock import flock_path
from .paths import home
from .resolver import UnknownReference

LIVE, DEAD, UNKNOWN = "live", "dead", "unknown"

# Reference tag that overrides `ref.provider` for picking a liveness adapter
# (e.g. a key stored under provider "gcp" that is really an Autumn key).
PROVIDER_TAG = "liveness_provider"

# Reference tag naming which credential of a multi-credential provider a
# reference holds (e.g. `private-token` vs `oauth-client-secret`). Without
# it the kind is read from the reference name's suffix.
KIND_TAG = "liveness_kind"

_DEFAULT_TIMEOUT = 10.0


class LivenessError(RuntimeError):
    """A liveness probe could not reach a verdict (network failure, no
    adapter, ...). Never carries a credential value or a response body."""


# transport(method, url, headers, timeout) -> HTTP status code
Transport = Callable[[str, str, Mapping[str, str], float], int]


def _default_transport(method: str, url: str, headers: Mapping[str, str], timeout: float) -> int:
    """urllib transport that returns the status code only. The body is never
    read: a provider error body can echo the key back (Autumn's
    `invalid_secret_key` response does), so it must not reach a result."""
    req = urllib.request.Request(url, headers=dict(headers), method=method)
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            return resp.status
    except urllib.error.HTTPError as exc:
        code = exc.code
        exc.close()
        return code
    except (urllib.error.URLError, OSError) as exc:
        reason = getattr(exc, "reason", exc)
        raise LivenessError(f"request failed: {type(reason).__name__}") from None


@dataclass(frozen=True)
class LivenessResult:
    """Outcome of one probe. Structurally incapable of carrying a value."""

    ref_name: str
    provider: str
    status: str                       # live | dead | unknown
    http_status: Optional[int] = None
    detail: str = ""
    checked_at: int = field(default_factory=lambda: int(time.time()))

    def to_dict(self) -> dict:
        return asdict(self)


@dataclass(frozen=True)
class HTTPProbe:
    """One cheap, read-only authenticated request and how to read its status.

    `dead_statuses` mean the provider rejected the credential itself. Any
    2xx is live. Anything else (429, 5xx, an unexpected 404) is unknown --
    never reported as dead, so a provider outage can't masquerade as a
    revoked key."""

    url: str
    method: str = "GET"
    header: str = "Authorization"
    scheme: str = "Bearer"           # "" sends the raw value in `header`
    dead_statuses: Tuple[int, ...] = (401, 403)
    query_param: str = ""            # set: send the value as this query parameter, no header

    def headers_for(self, value: str) -> Dict[str, str]:
        if self.query_param:
            return {}
        return {self.header: f"{self.scheme} {value}" if self.scheme else value}

    def url_for(self, value: str) -> str:
        """The request URL. Only called inside the resolver boundary: with
        `query_param` set it carries the value, so it must never reach a
        result's detail -- that uses `url`."""
        if not self.query_param:
            return self.url
        sep = "&" if "?" in self.url else "?"
        return f"{self.url}{sep}{urllib.parse.urlencode({self.query_param: value})}"

    def classify(self, code: int) -> str:
        if 200 <= code < 300:
            return LIVE
        if code in self.dead_statuses:
            return DEAD
        return UNKNOWN


class HTTPLivenessAdapter:
    """Generic adapter: resolve the reference at the boundary, send it as a
    header on `probe`, classify the status code."""

    def __init__(self, provider: str, probe: HTTPProbe, transport: Optional[Transport] = None,
                 timeout: float = _DEFAULT_TIMEOUT):
        self.provider = provider
        self.probe = probe
        self._transport = transport or _default_transport
        self._timeout = timeout

    def check(self, ref, resolver) -> LivenessResult:
        def boundary(value: str) -> int:
            return self._transport(
                self.probe.method, self.probe.url_for(value), self.probe.headers_for(value), self._timeout
            )

        code = resolver.resolve_call("{{secret:%s}}" % ref.name, boundary)
        status = self.probe.classify(code)
        return LivenessResult(
            ref_name=ref.name,
            provider=self.provider,
            status=status,
            http_status=code,
            detail=f"{self.probe.method} {self.probe.url} -> {code}",
        )


# provider -> zero-arg factory returning a liveness adapter. Provider
# sub-issues register their probes here (or via register_liveness_adapter).
_ADAPTERS: Dict[str, Callable[[], object]] = {}


def register_liveness_adapter(provider: str, factory: Callable[[], object]) -> None:
    """Register (or replace) the liveness adapter factory for `provider`."""
    _ADAPTERS[provider] = factory


def liveness_providers() -> List[str]:
    return sorted(_ADAPTERS)


def provider_for(ref) -> str:
    return (getattr(ref, "tags", None) or {}).get(PROVIDER_TAG) or getattr(ref, "provider", "") or ""


def liveness_adapter_for(provider: str):
    factory = _ADAPTERS.get(provider)
    return factory() if factory else None


def check_reference(ref, resolver, adapter=None) -> LivenessResult:
    """Probe one reference. Never raises for an expected failure -- policy
    refusals, missing adapters and network errors become `unknown` results
    with a reason, so an unattended sweep keeps going."""
    provider = provider_for(ref)
    if adapter is None:
        adapter = liveness_adapter_for(provider)
    if adapter is None:
        return LivenessResult(ref.name, provider, UNKNOWN,
                              detail=f"no liveness adapter for provider {provider or '(none)'!r}")
    try:
        return adapter.check(ref, resolver)
    except (NotInjectable, ApprovalRequired, NotAuthorized) as exc:
        return LivenessResult(ref.name, provider, UNKNOWN, detail=f"not resolvable: {type(exc).__name__}")
    except (BackendError, UnknownReference) as exc:
        return LivenessResult(ref.name, provider, UNKNOWN, detail=f"backend: {type(exc).__name__}")
    except LivenessError as exc:
        return LivenessResult(ref.name, provider, UNKNOWN, detail=str(exc))


def checkable_references(registry) -> List:
    """References a sweep should probe: a liveness adapter exists for their
    provider, it can probe this credential kind, and they are not
    dropped/revoked."""
    refs = []
    for ref in registry:
        if ref.state in ("dropped", "revoked"):
            continue
        adapter = liveness_adapter_for(provider_for(ref))
        if adapter is None:
            continue
        # A provider's non-probeable credentials (OAuth client secrets, ...)
        # would only ever read unknown, so a sweep leaves them out; an
        # explicit `check <name>` still explains why.
        can_probe = getattr(adapter, "can_probe", None)
        if can_probe is not None and not can_probe(ref):
            continue
        refs.append(ref)
    return refs


def audit_liveness(audit, ref, result: LivenessResult) -> None:
    tag = result.status if result.http_status is None else f"{result.status}:{result.http_status}"
    audit.append("liveness", ref.sm_name, tag)


# ---------------------------------------------------------------------------
# Persisted status -- the escalation clock.
# ---------------------------------------------------------------------------


@dataclass
class LivenessStatus:
    ref_name: str
    provider: str = ""
    status: str = UNKNOWN
    http_status: Optional[int] = None
    detail: str = ""
    last_checked_at: Optional[int] = None
    last_live_at: Optional[int] = None
    first_failed_at: Optional[int] = None   # set on the first non-live result, cleared by a live one
    consecutive_failures: int = 0

    def to_dict(self) -> dict:
        return asdict(self)


def _status_path(path: Optional[Path] = None) -> Path:
    return path or (home() / "liveness-status.json")


def _lock_path(path: Optional[Path] = None) -> Path:
    return _status_path(path).with_suffix(".lock")


def load_liveness_status(path: Optional[Path] = None) -> Dict[str, LivenessStatus]:
    status_path = _status_path(path)
    if not status_path.exists():
        return {}
    raw = json.loads(status_path.read_text() or "{}")
    return {name: LivenessStatus(ref_name=name, **{k: v for k, v in cfg.items() if k != "ref_name"})
            for name, cfg in raw.items()}


def _apply(statuses: Dict[str, LivenessStatus], result: LivenessResult) -> None:
    st = statuses.get(result.ref_name) or LivenessStatus(ref_name=result.ref_name)
    st.provider = result.provider
    st.status = result.status
    st.http_status = result.http_status
    st.detail = result.detail
    st.last_checked_at = result.checked_at
    if result.status == LIVE:
        st.last_live_at = result.checked_at
        st.first_failed_at = None
        st.consecutive_failures = 0
    else:
        if st.first_failed_at is None:
            st.first_failed_at = result.checked_at
        st.consecutive_failures += 1
    statuses[result.ref_name] = st


def record_results(results: Iterable[LivenessResult], path: Optional[Path] = None) -> Dict[str, LivenessStatus]:
    """Locked read-modify-write of liveness-status.json."""
    with flock_path(_lock_path(path)):
        statuses = load_liveness_status(path)
        for result in results:
            _apply(statuses, result)
        atomic_write(_status_path(path), json.dumps(
            {name: st.to_dict() for name, st in statuses.items()}, indent=2))
        return statuses


def failing(statuses: Mapping[str, LivenessStatus]) -> List[LivenessStatus]:
    """Every reference whose last check was not live, oldest failure first."""
    bad = [st for st in statuses.values() if st.status != LIVE]
    return sorted(bad, key=lambda st: (st.first_failed_at or 0, st.ref_name))


# ---------------------------------------------------------------------------
# Cross-system parity.
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class ParityResult:
    """Which targets agree. `groups` lists target names partitioned by
    equal value (largest group first); `errors` maps an unreadable target to
    the exception type name. Never a value or a reusable fingerprint."""

    ref_name: str
    groups: Tuple[Tuple[str, ...], ...]
    errors: Dict[str, str] = field(default_factory=dict)

    @property
    def in_sync(self) -> bool:
        return len(self.groups) == 1 and not self.errors

    def to_dict(self) -> dict:
        return {"ref_name": self.ref_name, "in_sync": self.in_sync,
                "groups": [list(g) for g in self.groups], "errors": dict(self.errors)}


def parity_check(ref_name: str, fetchers: Mapping[str, Callable[[], str]]) -> ParityResult:
    """Fetch `ref_name` from every target and group targets by equality.

    Each fetcher returns the target's live value; it is HMAC'd under a key
    generated for this call alone and the plaintext reference is dropped
    before the next fetch."""
    key = secrets.token_bytes(32)
    digests: Dict[str, bytes] = {}
    errors: Dict[str, str] = {}
    for target, fetch in fetchers.items():
        try:
            value = fetch()
        except Exception as exc:  # any target failure is reported, never fatal
            errors[target] = type(exc).__name__
            continue
        digests[target] = hmac.new(key, value.encode(), hashlib.sha256).digest()
        del value

    groups: List[List[str]] = []
    group_digests: List[bytes] = []
    for target, digest in digests.items():
        for i, gd in enumerate(group_digests):
            if hmac.compare_digest(gd, digest):
                groups[i].append(target)
                break
        else:
            group_digests.append(digest)
            groups.append([target])
    groups.sort(key=lambda g: (-len(g), g))
    return ParityResult(ref_name, tuple(tuple(g) for g in groups), errors)


def backend_fetcher(backend, sm_name: str, project: str = "") -> Callable[[], str]:
    """Fetcher for any Portunus SecretBackend (local vault, GCP, ...)."""
    return lambda: backend.access(sm_name, project=project)


# ---------------------------------------------------------------------------
# Built-in provider probes.
# ---------------------------------------------------------------------------


class GitHubLivenessAdapter(HTTPLivenessAdapter):
    """GitHub (PANT-1141): `GET /rate_limit` with the stored token. It doesn't
    count against the REST rate limit and accepts every bearer-token type
    (classic/fine-grained PAT, OAuth App, App user, installation), so one
    probe covers them all. Only 401 ("Bad credentials") is dead -- GitHub's
    403 also means rate-limited or forbidden, so it stays unknown. A stored
    `ghs_` installation token reads dead after its 1-hour expiry, which is
    the truth about that stored copy.

    A GitHub App private key is not a bearer token (it signs a JWT), so
    sending it would read as a false "dead" -- it is reported unknown
    without any request, whether identified by the `github_token_type` tag
    or by its PEM header inside the resolver boundary."""

    PRIVATE_KEY = -1  # boundary sentinel: never a real HTTP status

    def __init__(self, transport: Optional[Transport] = None, timeout: float = _DEFAULT_TIMEOUT):
        super().__init__(
            "github",
            HTTPProbe(url="https://api.github.com/rate_limit", dead_statuses=(401,)),
            transport=transport,
            timeout=timeout,
        )

    def check(self, ref, resolver) -> LivenessResult:
        not_bearer = LivenessResult(ref.name, self.provider, UNKNOWN,
                                    detail="GitHub App private key -- not a bearer token, not probed")
        if (getattr(ref, "tags", None) or {}).get("github_token_type") == "app_private_key":
            return not_bearer

        def boundary(value: str) -> int:
            value = value.strip()
            if value.startswith("-----BEGIN"):
                return self.PRIVATE_KEY
            return self._transport(
                self.probe.method, self.probe.url, self.probe.headers_for(value), self._timeout
            )

        code = resolver.resolve_call("{{secret:%s}}" % ref.name, boundary)
        if code == self.PRIVATE_KEY:
            return not_bearer
        return LivenessResult(
            ref_name=ref.name,
            provider=self.provider,
            status=self.probe.classify(code),
            http_status=code,
            detail=f"{self.probe.method} {self.probe.url} -> {code}",
        )


register_liveness_adapter("github", GitHubLivenessAdapter)


def credential_kind(ref, kinds: Iterable[str]) -> str:
    """Which of a provider's credential `kinds` this reference holds: the
    `liveness_kind` tag if set, else the longest kind the reference name ends
    with (`event-api-prod-eventbrite-private-token` -> `private-token`).
    Empty when neither says."""
    tagged = (getattr(ref, "tags", None) or {}).get(KIND_TAG)
    if tagged:
        return tagged
    for kind in sorted(kinds, key=len, reverse=True):
        if ref.name == kind or ref.name.endswith("-" + kind):
            return kind
    return ""


class CredentialKindLivenessAdapter(HTTPLivenessAdapter):
    """For providers that issue several credentials of which only some can be
    sent on their own (PANT-1126). Only references whose `credential_kind`
    is in `probe_kinds` are probed. The rest -- OAuth client secrets, client
    ids that only work inside an OAuth exchange, retired key types -- read
    `unknown` with the reason from `not_probed` and no request, because
    sending them on the probe would be rejected and read as a false "dead".
    An unrecognised kind is never guessed at: it is not probed either."""

    def __init__(self, provider: str, probe: Optional[HTTPProbe], probe_kinds: Tuple[str, ...],
                 not_probed: Mapping[str, str], transport: Optional[Transport] = None,
                 timeout: float = _DEFAULT_TIMEOUT):
        super().__init__(provider, probe, transport=transport, timeout=timeout)
        self.probe_kinds = probe_kinds
        self.not_probed = dict(not_probed)

    def kind(self, ref) -> str:
        return credential_kind(ref, tuple(self.probe_kinds) + tuple(self.not_probed))

    def can_probe(self, ref) -> bool:
        return self.probe is not None and self.kind(ref) in self.probe_kinds

    def check(self, ref, resolver) -> LivenessResult:
        if not self.can_probe(ref):
            kind = self.kind(ref)
            reason = self.not_probed.get(kind) or (
                f"credential kind {kind or '(unrecognised)'!r} has no probe; set the "
                f"{KIND_TAG!r} tag to one of: {', '.join(self.probe_kinds) or '(none)'}"
            )
            return LivenessResult(ref.name, self.provider, UNKNOWN, detail=f"not probed: {reason}")
        return super().check(ref, resolver)


class EventbriteLivenessAdapter(CredentialKindLivenessAdapter):
    """Eventbrite: `GET /v3/users/me/` with the private token as a bearer
    token. 401 (`INVALID_AUTH`) is dead; a 403 can be a scope problem on a
    working token, so it stays unknown. The app's API key and OAuth client
    credentials are not bearer tokens."""

    def __init__(self, transport: Optional[Transport] = None, timeout: float = _DEFAULT_TIMEOUT):
        super().__init__(
            "eventbrite",
            HTTPProbe(url="https://www.eventbriteapi.com/v3/users/me/", dead_statuses=(401,)),
            probe_kinds=("private-token",),
            not_probed={
                "api-key": "the Eventbrite API key is the app's client key, not a bearer token "
                           "(tag liveness_kind=private-token if this one is a personal token)",
                "oauth-client-id": "OAuth client id -- only usable inside an OAuth code exchange",
                "oauth-client-secret": "OAuth client secret -- only usable inside an OAuth code exchange",
            },
            transport=transport, timeout=timeout,
        )


class TicketmasterLivenessAdapter(CredentialKindLivenessAdapter):
    """Ticketmaster: the consumer key is the Discovery API `apikey` query
    parameter. `classifications.json?size=1` is a small authenticated read;
    an invalid key gets 401 (`oauth.v2.InvalidApiKey`). A 403 can mean the
    key works but lacks this API product, so it stays unknown. The consumer
    secret is never sent on Discovery requests."""

    def __init__(self, transport: Optional[Transport] = None, timeout: float = _DEFAULT_TIMEOUT):
        super().__init__(
            "ticketmaster",
            HTTPProbe(url="https://app.ticketmaster.com/discovery/v2/classifications.json?size=1",
                      dead_statuses=(401,), query_param="apikey"),
            probe_kinds=("consumer-key",),
            not_probed={
                "consumer-secret": "the consumer secret is not accepted on its own by any read-only endpoint",
            },
            transport=transport, timeout=timeout,
        )


class SeatGeekLivenessAdapter(CredentialKindLivenessAdapter):
    """SeatGeek: the client id is the `client_id` query parameter on the
    public Platform API. An invalid one gets 403 `Invalid client credentials`
    (code 40302). The client secret is optional on reads and only valid next
    to its id, so a single reference can't be probed for it."""

    def __init__(self, transport: Optional[Transport] = None, timeout: float = _DEFAULT_TIMEOUT):
        super().__init__(
            "seatgeek",
            HTTPProbe(url="https://api.seatgeek.com/2/taxonomies", dead_statuses=(401, 403),
                      query_param="client_id"),
            probe_kinds=("client-id",),
            not_probed={
                "client-secret": "the client secret is only checked alongside its client id -- "
                                 "no single-reference probe",
            },
            transport=transport, timeout=timeout,
        )


class MeetupLivenessAdapter(CredentialKindLivenessAdapter):
    """Meetup: nothing event-api stores can be probed on its own. Meetup
    retired API-key auth on 2019-08-15 (the key endpoints now 404), and its
    GraphQL API answers an invalid OAuth access token with HTTP 200 and an
    `access_token_invalid` error in the body, which this module never reads.
    Registered so `check` explains this instead of "no adapter"; a sweep
    skips Meetup references."""

    def __init__(self, transport: Optional[Transport] = None, timeout: float = _DEFAULT_TIMEOUT):
        super().__init__(
            "meetup",
            None,
            probe_kinds=(),
            not_probed={
                "key": "Meetup retired API-key auth on 2019-08-15 -- a stored key cannot work; "
                       "see docs/rotation.md (Meetup)",
                "client-id": "OAuth client id -- only usable inside an OAuth exchange",
                "client-secret": "OAuth client secret -- only usable inside an OAuth exchange",
            },
            transport=transport, timeout=timeout,
        )


register_liveness_adapter("eventbrite", EventbriteLivenessAdapter)
register_liveness_adapter("ticketmaster", TicketmasterLivenessAdapter)
register_liveness_adapter("seatgeek", SeatGeekLivenessAdapter)
register_liveness_adapter("meetup", MeetupLivenessAdapter)
