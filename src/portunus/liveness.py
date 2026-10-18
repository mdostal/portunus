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
  fingerprint that could be compared across runs. A `digesting` fetcher
  digests inside a resolver boundary instead, so the value never comes back
  up the stack; `parity_fetchers()` builds them from target specs
  (`ref`, `gcp`, `convex:<deploy-key-ref>:<VAR>`, ...) for `portunus parity`.

Built-in provider probes are registered at the bottom of this module by the
provider sub-issues (GitHub, PANT-1141, is the first; Neon, PANT-1120, probes a
Postgres connection string instead of an HTTP API; the event-api sources
Eventbrite, Ticketmaster, SeatGeek and Meetup, PANT-1126, probe only the
credential kinds that can be sent on their own; PANT-1125 adds the
long-tail single-API-key SaaS providers; Vercel, PANT-1149, reads only the
`invalidToken` flag and the token's expiry from its response; Convex, PANT-1151,
probes a deploy key and reads deployment env vars for parity). See docs/liveness.md.
"""
from __future__ import annotations

import hashlib
import hmac
import json
import re
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
    `invalid_secret_key` response does), so it must not reach a result.

    Sends its own User-Agent: Pexels and Pixabay answer urllib's default
    `Python-urllib/x.y` with 403 before looking at the key."""
    headers = {"User-Agent": "portunus-liveness", **headers}
    req = urllib.request.Request(url, headers=headers, method=method)
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


def digesting(fetch: Callable[[Callable[[str], bytes]], bytes]) -> Callable:
    """Mark `fetch` as a digesting fetcher: `parity_check` calls it with the
    per-call `digest(value) -> bytes` function and it returns the digest
    itself. That lets the value be digested inside a resolver boundary, so
    the plaintext never comes back up the stack at all."""
    fetch.parity_digest = True
    return fetch


def parity_check(ref_name: str, fetchers: Mapping[str, Callable]) -> ParityResult:
    """Fetch `ref_name` from every target and group targets by equality.

    A plain fetcher returns the target's live value; it is HMAC'd under a
    key generated for this call alone and the plaintext reference is dropped
    before the next fetch. A `digesting` fetcher is handed that HMAC and
    returns the digest instead of the value."""
    key = secrets.token_bytes(32)

    def digest(value: str) -> bytes:
        return hmac.new(key, value.encode(), hashlib.sha256).digest()

    digests: Dict[str, bytes] = {}
    errors: Dict[str, str] = {}
    for target, fetch in fetchers.items():
        try:
            if getattr(fetch, "parity_digest", False):
                got = fetch(digest)
                if not isinstance(got, bytes) or len(got) != hashlib.sha256().digest_size:
                    raise TypeError("digesting fetcher did not return a digest")
                digests[target] = got
            else:
                value = fetch()
                digests[target] = digest(value)
                del value
        except Exception as exc:  # any target failure is reported, never fatal
            errors[target] = type(exc).__name__
            continue

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


def reference_fetcher(resolver, ref_name: str) -> Callable:
    """Digesting fetcher for a reference's own value, through the resolver:
    broker policy runs first (a gated or dropped reference is an error, not
    a read) and the value is digested inside the boundary."""
    return digesting(lambda digest: resolver.resolve_call("{{secret:%s}}" % ref_name, digest))


def policy_backend_fetcher(broker, backend, ref, project: str = "") -> Callable:
    """Digesting fetcher for `ref`'s copy in one specific backend (say GCP
    Secret Manager when the reference normally resolves from the local
    vault). The broker's policy check runs first, as it does for a resolve,
    so parity can't be used to read around a gate."""
    from .broker import Identity

    def fetch(digest):
        checked = broker.check_injectable(ref.name, requester=Identity.from_env())
        value = backend.access(checked.sm_name, project=project or checked.project)
        try:
            return digest(value)
        finally:
            del value

    return digesting(fetch)


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


# connector(conninfo, timeout) -> None on success; raises pgwire.PgError.
PgConnector = Callable[[object, float], None]


class NeonLivenessAdapter:
    """Neon Postgres (PANT-1120): the stored value is a connection string, so
    the probe is a real connect + `SELECT 1` against it (portunus.pgwire, no
    driver dependency) rather than an HTTP call. It is the only check that
    catches the 2026-10-16 incident shape: Neon rotated a branch role's
    password server-side and the stored string sat broken until someone
    next used it.

    SQLSTATE class 28 (`28P01` password authentication failed, `28000`) is
    dead. Anything else -- network, TLS, a suspended or deleted endpoint, a
    missing database, a value that is not a connection string -- is unknown.
    Only the host and the SQLSTATE reach the result: never the user, the
    password, the query string or server message text. `http_status` stays
    None; this is not an HTTP probe.

    Works for any Postgres connection string, not only Neon's."""

    provider = "neon"

    def __init__(self, connector: Optional[PgConnector] = None, timeout: float = _DEFAULT_TIMEOUT):
        from . import pgwire
        self._connector = connector or pgwire.probe
        self._timeout = timeout

    def check(self, ref, resolver) -> LivenessResult:
        from . import pgwire

        def boundary(value: str) -> Tuple[str, str]:
            try:
                info = pgwire.parse_url(value)
            except ValueError as exc:
                return UNKNOWN, f"stored value is not usable: {exc}"
            target = f"{info.host}/{info.database}"
            try:
                self._connector(info, self._timeout)
            except pgwire.PgError as exc:
                status = DEAD if exc.auth_failed else UNKNOWN
                code = f"SQLSTATE {exc.sqlstate}" if exc.sqlstate else str(exc)
                return status, f"SELECT 1 @ {target} -> {code}"
            return LIVE, f"SELECT 1 @ {target} -> ok"

        status, detail = resolver.resolve_call("{{secret:%s}}" % ref.name, boundary)
        return LivenessResult(ref.name, self.provider, status, detail=detail)


register_liveness_adapter("neon", NeonLivenessAdapter)


# ---------------------------------------------------------------------------
# Long-tail single-API-key SaaS providers (PANT-1125, GH #204). Each is one
# cheap authenticated read; every endpoint returned 401 (Pixabay: 400) for a
# bogus key when this was written. None of them returns 401 for a rate
# limit, so 429/5xx stay unknown as usual.
# ---------------------------------------------------------------------------

LONG_TAIL_PROBES: Dict[str, HTTPProbe] = {
    # Lists the account's projects. `browserbase-project-id` is config, not a key.
    "browserbase": HTTPProbe(url="https://api.browserbase.com/v1/projects",
                             header="X-BB-API-Key", scheme="", dead_statuses=(401,)),
    # Remaining credits for the key's team; scrapes nothing.
    "firecrawl": HTTPProbe(url="https://api.firecrawl.dev/v2/team/credit-usage", dead_statuses=(401,)),
    # Lists checks; works with read-only keys too. healthchecks.io only: a
    # self-hosted instance needs its own probe. Ping keys/URLs are not
    # probed -- a request to them records a ping.
    "healthchecks": HTTPProbe(url="https://healthchecks.io/api/v3/checks/",
                              header="X-Api-Key", scheme="", dead_statuses=(401,)),
    # One curated photo. Counts against the 200/hour request limit.
    "pexels": HTTPProbe(url="https://api.pexels.com/v1/curated?per_page=1", scheme="",
                       dead_statuses=(401,)),
    # Key only accepted as `?key=`. An invalid key is a plain 400 (the
    # request is otherwise fixed and valid), so 400 is dead here.
    "pixabay": HTTPProbe(url="https://pixabay.com/api/?per_page=3",
                         query_param="key", dead_statuses=(400,)),
    # Account API: free, not counted toward the search quota. Key only
    # accepted as `?api_key=`.
    "serpapi": HTTPProbe(url="https://serpapi.com/account.json",
                         query_param="api_key", dead_statuses=(401,)),
    # One address. Test and live tokens share the same host.
    "shippo": HTTPProbe(url="https://api.goshippo.com/addresses?results=1",
                        scheme="ShippoToken", dead_statuses=(401,)),
    # Vercel AI Gateway API key (not the Vercel deploy token): the team's
    # credit balance.
    "vercel-ai": HTTPProbe(url="https://ai-gateway.vercel.sh/v1/credits", dead_statuses=(401,)),
}


def _register_long_tail(provider: str, probe: HTTPProbe) -> None:
    register_liveness_adapter(provider, lambda: HTTPLivenessAdapter(provider, probe))


for _provider, _probe in LONG_TAIL_PROBES.items():
    _register_long_tail(_provider, _probe)


class TremendousLivenessAdapter(HTTPLivenessAdapter):
    """Tremendous: `GET /api/v2/funding_sources`. Sandbox and production
    keys live on different hosts, and a key sent to the wrong one reads
    401, a false dead. The host is picked from the key prefix (`TEST_` ->
    testflight, `PROD_` -> api) inside the boundary, or from the
    `tremendous_env` tag (`sandbox` / `production`) for an older key
    without a prefix. With neither, the key is reported unknown without a
    request."""

    HOSTS = {"sandbox": "https://testflight.tremendous.com",
             "production": "https://api.tremendous.com"}
    PATH = "/api/v2/funding_sources"
    NO_ENV = -1  # boundary sentinel: never a real HTTP status

    def __init__(self, transport: Optional[Transport] = None, timeout: float = _DEFAULT_TIMEOUT):
        super().__init__("tremendous",
                         HTTPProbe(url=self.HOSTS["production"] + self.PATH, dead_statuses=(401,)),
                         transport=transport, timeout=timeout)

    def check(self, ref, resolver) -> LivenessResult:
        tagged = (getattr(ref, "tags", None) or {}).get("tremendous_env", "")
        used = {}

        def boundary(value: str) -> int:
            value = value.strip()
            env = ("sandbox" if value.startswith("TEST_")
                   else "production" if value.startswith("PROD_") else tagged)
            if env not in self.HOSTS:
                return self.NO_ENV
            used["url"] = self.HOSTS[env] + self.PATH
            return self._transport("GET", used["url"], self.probe.headers_for(value), self._timeout)

        code = resolver.resolve_call("{{secret:%s}}" % ref.name, boundary)
        if code == self.NO_ENV:
            return LivenessResult(ref.name, self.provider, UNKNOWN,
                                  detail="no TEST_/PROD_ prefix and no tremendous_env tag -- not probed")
        return LivenessResult(ref.name, self.provider, self.probe.classify(code), http_status=code,
                              detail=f"GET {used['url']} -> {code}")


register_liveness_adapter("tremendous", TremendousLivenessAdapter)


# ---------------------------------------------------------------------------
# Vercel deploy/access tokens (PANT-1149, GH #144).
# ---------------------------------------------------------------------------

# transport(method, url, headers, timeout) -> (HTTP status, facts). `facts`
# holds only the two non-secret fields the Vercel adapter reads from the
# body -- {"invalid_token": bool, "expires_at": epoch-ms or None} -- never
# the body itself.
VercelTransport = Callable[[str, str, Mapping[str, str], float], Tuple[int, Dict[str, object]]]

_VERCEL_BODY_LIMIT = 64 * 1024


def _vercel_facts(raw: bytes) -> Dict[str, object]:
    """The only two things the Vercel adapter needs from a response body.
    The parsed body is dropped here; nothing else leaves this function."""
    try:
        data = json.loads(raw.decode("utf-8", "replace"))
    except ValueError:
        data = None
    if not isinstance(data, dict):
        data = {}
    err = data.get("error") if isinstance(data.get("error"), dict) else {}
    token = data.get("token") if isinstance(data.get("token"), dict) else {}
    expires_at = token.get("expiresAt")
    return {
        "invalid_token": err.get("invalidToken") is True,
        "expires_at": expires_at if isinstance(expires_at, int) and not isinstance(expires_at, bool) else None,
    }


def _default_vercel_transport(method: str, url: str, headers: Mapping[str, str],
                              timeout: float) -> Tuple[int, Dict[str, object]]:
    """Like `_default_transport`, but reads the (bounded) body for the two
    facts `_vercel_facts` extracts. Vercel's error body is a fixed
    `{"error": {"code", "message", "invalidToken"}}` and its token metadata
    has no secret in it, but only a bool and an int come out regardless."""
    headers = {"User-Agent": "portunus-liveness", **headers}
    req = urllib.request.Request(url, headers=headers, method=method)
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            return resp.status, _vercel_facts(resp.read(_VERCEL_BODY_LIMIT))
    except urllib.error.HTTPError as exc:
        try:
            facts = _vercel_facts(exc.read(_VERCEL_BODY_LIMIT))
        except OSError:
            facts = _vercel_facts(b"")
        finally:
            exc.close()
        return exc.code, facts
    except (urllib.error.URLError, OSError) as exc:
        reason = getattr(exc, "reason", exc)
        raise LivenessError(f"request failed: {type(reason).__name__}") from None


class VercelLivenessAdapter(HTTPLivenessAdapter):
    """Vercel (PANT-1149, GH #144): `GET /v5/user/tokens/current`, the
    metadata of the token sending the request -- read-only, no project or
    deployment data, the same call the rotation adapter verifies with.

    Vercel answers every auth failure with 403, including a request with no
    token at all (`missingToken`), so a bare 403 is not proof the token is
    dead. Dead is 401, or 403 with `error.invalidToken: true` -- what the
    dead `vercel-deploy-token` returned on 2026-09-13. Any other 403 is
    unknown.

    A live token's `expiresAt` goes into the result's detail, with a
    warning once it is within `EXPIRY_WARN_DAYS`: a Vercel token can only
    be rotated (`portunus rotation run`, VercelRotationAdapter) while it still
    works, so expiry has to be acted on before it happens."""

    URL = "https://api.vercel.com/v5/user/tokens/current"
    EXPIRY_WARN_DAYS = 14

    def __init__(self, transport: Optional[VercelTransport] = None, timeout: float = _DEFAULT_TIMEOUT,
                 now: Callable[[], float] = time.time):
        super().__init__("vercel", HTTPProbe(url=self.URL, dead_statuses=(401,)), timeout=timeout)
        self._transport = transport or _default_vercel_transport
        self._now = now

    def check(self, ref, resolver) -> LivenessResult:
        def boundary(value: str) -> Tuple[int, Dict[str, object]]:
            return self._transport(
                self.probe.method, self.probe.url, self.probe.headers_for(value.strip()), self._timeout
            )

        code, facts = resolver.resolve_call("{{secret:%s}}" % ref.name, boundary)
        detail = f"{self.probe.method} {self.probe.url} -> {code}"
        status = self.probe.classify(code)
        if code == 403:
            if facts.get("invalid_token"):
                status = DEAD
                detail += " (invalidToken)"
            else:
                detail += " (no invalidToken flag; not proof the token is dead)"
        elif status == LIVE and facts.get("expires_at"):
            detail += "; " + self._expiry(int(facts["expires_at"]), ref.name)
        return LivenessResult(ref.name, self.provider, status, http_status=code, detail=detail)

    def _expiry(self, expires_at_ms: int, ref_name: str) -> str:
        expires = expires_at_ms / 1000
        day = time.strftime("%Y-%m-%d", time.gmtime(expires))
        days = int((expires - self._now()) // 86400)
        if days >= self.EXPIRY_WARN_DAYS:
            return f"expires {day} (in {days}d)"
        return (f"EXPIRES {day} (in {max(days, 0)}d) -- rotate before then: "
                f"portunus rotation run {ref_name}")


register_liveness_adapter("vercel", VercelLivenessAdapter)


# ---------------------------------------------------------------------------
# Convex (PANT-1151, GH #142): deploy-key liveness and a parity fetcher for
# the env vars a Convex deployment holds. Both talk to the deployment's own
# HTTP API with `Authorization: Convex <deploy key>`, the auth the Convex CLI
# and the Deployment Platform API use. No `npx convex` subprocess.
# ---------------------------------------------------------------------------

# Reference tag on a deploy-key reference: the deployment URL, for keys that
# don't name their deployment (preview / project keys) or a deployment that
# is not at the default `https://<name>.convex.cloud`.
CONVEX_URL_TAG = "convex_url"

_CONVEX_NAME_CHARS = frozenset("abcdefghijklmnopqrstuvwxyz0123456789-")


def convex_deployment_url(value: str, override: str = "") -> str:
    """The deployment URL a Convex key authenticates against, or "".

    Called inside the resolver boundary. Only the part before `|` is read:
    `prod:<name>|...` and `dev:<name>|...` deploy keys and `<name>|...`
    admin keys name their deployment, which is public (it is the client's
    CONVEX_URL). `preview:` and `project:` keys name a team and project, not
    a deployment, so they need `override` (the `convex_url` tag)."""
    if override:
        return override.rstrip("/")
    head, sep, _ = value.strip().partition("|")
    if not sep:
        return ""
    parts = head.split(":")
    if len(parts) == 1:
        name = parts[0]
    elif len(parts) == 2 and parts[0] in ("prod", "dev"):
        name = parts[1]
    else:
        return ""
    if not name or not set(name) <= _CONVEX_NAME_CHARS:
        return ""
    return f"https://{name}.convex.cloud"


def _convex_headers(value: str) -> Dict[str, str]:
    return {"Authorization": f"Convex {value.strip()}"}


class ConvexLivenessAdapter(CredentialKindLivenessAdapter):
    """Convex: `GET <deployment>/api/check_admin_key` with the stored deploy
    key. It is the endpoint the dashboard uses to validate a key: it reads
    nothing from the deployment (no data, no env vars) and answers a valid
    key of any permission level with 200. A key for another deployment, or
    a deleted one, gets 403 `BadDeployKey`; a malformed one gets 401.

    Only `*-deploy-key` / `*-admin-key` references are probed (or a
    `liveness_kind` tag). The deployment comes from the key itself, or from
    the `convex_url` tag. With neither (a preview or project key with no
    tag) nothing is sent: a request to the wrong deployment would read as a
    false dead."""

    PATH = "/api/check_admin_key"
    NO_URL = -1  # boundary sentinel: never a real HTTP status

    def __init__(self, transport: Optional[Transport] = None, timeout: float = _DEFAULT_TIMEOUT):
        super().__init__(
            "convex",
            HTTPProbe(url="https://<deployment>.convex.cloud" + self.PATH, header="Authorization",
                      scheme="Convex", dead_statuses=(401, 403)),
            probe_kinds=("deploy-key", "admin-key"),
            not_probed={
                "url": "deployment URL -- config, not a credential",
                "deployment": "deployment name -- config, not a credential",
            },
            transport=transport, timeout=timeout,
        )

    def check(self, ref, resolver) -> LivenessResult:
        if not self.can_probe(ref):
            return super().check(ref, resolver)
        override = (getattr(ref, "tags", None) or {}).get(CONVEX_URL_TAG, "")
        used = {}

        def boundary(value: str) -> int:
            base = convex_deployment_url(value, override)
            if not base:
                return self.NO_URL
            used["url"] = base + self.PATH
            return self._transport("GET", used["url"], _convex_headers(value), self._timeout)

        code = resolver.resolve_call("{{secret:%s}}" % ref.name, boundary)
        if code == self.NO_URL:
            return LivenessResult(ref.name, self.provider, UNKNOWN,
                                  detail=f"key names no deployment and no {CONVEX_URL_TAG!r} tag -- not probed")
        return LivenessResult(ref.name, self.provider, self.probe.classify(code), http_status=code,
                              detail=f"GET {used['url']} -> {code}")


register_liveness_adapter("convex", ConvexLivenessAdapter)


class ConvexEnvError(RuntimeError):
    """Reading a Convex deployment's env vars failed. Carries the HTTP status
    at most -- never a key, a value or a response body."""


class ConvexEnvVarMissing(ConvexEnvError):
    """The deployment has no env var of that name."""


# json_transport(method, url, headers, timeout) -> (status, body). The body
# is only read for a 2xx; an error body is never read.
JSONTransport = Callable[[str, str, Mapping[str, str], float], Tuple[int, bytes]]


def _default_json_transport(method: str, url: str, headers: Mapping[str, str],
                            timeout: float) -> Tuple[int, bytes]:
    headers = {"User-Agent": "portunus-liveness", **headers}
    req = urllib.request.Request(url, headers=headers, method=method)
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            return resp.status, resp.read()
    except urllib.error.HTTPError as exc:
        code = exc.code
        exc.close()
        return code, b""
    except (urllib.error.URLError, OSError) as exc:
        reason = getattr(exc, "reason", exc)
        raise ConvexEnvError(f"request failed: {type(reason).__name__}") from None


def convex_env_fetcher(resolver, deploy_key_ref, var: str,
                       transport: Optional[JSONTransport] = None,
                       timeout: float = _DEFAULT_TIMEOUT) -> Callable:
    """Digesting parity fetcher for env var `var` on the Convex deployment
    that `deploy_key_ref` (a registered deploy key) authenticates against.

    Uses the Deployment Platform API's `GET /api/v1/list_environment_variables`
    (Convex has no single-variable read on that API). Everything happens
    inside the deploy key's resolver boundary: the response is parsed there,
    `var` is digested there, and the rest of the response is dropped. Only
    the digest leaves. A non-200 raises with the status code alone."""
    send = transport or _default_json_transport
    override = (getattr(deploy_key_ref, "tags", None) or {}).get(CONVEX_URL_TAG, "")

    def fetch(digest):
        def boundary(key: str) -> bytes:
            base = convex_deployment_url(key, override)
            if not base:
                raise ConvexEnvError(f"deploy key names no deployment and no {CONVEX_URL_TAG!r} tag")
            code, body = send("GET", base + "/api/v1/list_environment_variables",
                              _convex_headers(key), timeout)
            if code != 200:
                raise ConvexEnvError(f"list_environment_variables -> {code}")
            try:
                env = json.loads(body)["environmentVariables"]
            except (ValueError, KeyError, TypeError):
                raise ConvexEnvError("unexpected list_environment_variables response") from None
            finally:
                del body
            value = env.get(var) if isinstance(env, dict) else None
            del env
            if not isinstance(value, str):
                raise ConvexEnvVarMissing(f"no env var {var!r} on the deployment")
            try:
                return digest(value)
            finally:
                del value

        return resolver.resolve_call("{{secret:%s}}" % deploy_key_ref.name, boundary)

    return digesting(fetch)


# ---------------------------------------------------------------------------
# Parity targets: where else a reference's value is supposed to live.
# ---------------------------------------------------------------------------

# Reference tag listing a reference's parity targets, separated by spaces
# (`--tags` splits on commas, so a comma can't be set from the CLI; commas
# and semicolons are accepted too), e.g.
# `gcp convex:social-engine-prod-convex-deploy-key:AUTUMN_SECRET_KEY`.
PARITY_TAG = "parity_targets"
_SPEC_SEP = re.compile(r"[\s,;]+")


class ParityTargetError(ValueError):
    """A parity target spec is malformed or names something unknown."""


def parity_specs(ref) -> List[str]:
    raw = (getattr(ref, "tags", None) or {}).get(PARITY_TAG, "")
    return [s for s in _SPEC_SEP.split(raw) if s]


def parity_references(registry) -> List:
    """References a parity sweep covers: a `parity_targets` tag, not
    dropped/revoked."""
    return [ref for ref in registry
            if parity_specs(ref) and ref.state not in ("dropped", "revoked")]


def _convex_var_for(ref) -> str:
    """The env var name a reference is injected as, when there is exactly one."""
    names = list((getattr(ref, "injected_as", None) or {}).keys())
    return names[0] if len(names) == 1 else ""


def parity_fetchers(ref, specs: Iterable[str], registry, broker, resolver,
                    backend_for_kind: Callable[[str], object],
                    convex_transport: Optional[JSONTransport] = None) -> Dict[str, Callable]:
    """Build one fetcher per target spec. Specs:

    - `ref` -- the reference's own value, resolved as normal (its routed backend)
    - `<backend>[:<project>]` -- its copy in that backend (`gcp`, `local`, ...);
      the project defaults to the reference's own
    - `convex:<deploy-key-ref>[:<VAR>]` -- env var VAR on the Convex deployment
      that deploy key belongs to; VAR defaults to the reference's single
      `injected_as` name

    Raises ParityTargetError for a malformed spec, so a typo is reported
    rather than read as drift."""
    fetchers: Dict[str, Callable] = {}
    for spec in specs:
        kind, _, rest = spec.partition(":")
        if spec == "ref":
            fetchers[spec] = reference_fetcher(resolver, ref.name)
        elif kind == "convex":
            key_name, _, var = rest.partition(":")
            key_ref = registry.get(key_name) if key_name else None
            if key_ref is None:
                raise ParityTargetError(f"{spec!r}: no deploy-key reference {key_name!r} in registry")
            var = var or _convex_var_for(ref)
            if not var:
                raise ParityTargetError(f"{spec!r}: name the env var (convex:<deploy-key-ref>:<VAR>)")
            fetchers[spec] = convex_env_fetcher(resolver, key_ref, var, transport=convex_transport)
        else:
            try:
                backend = backend_for_kind(kind)
            except KeyError:
                raise ParityTargetError(f"{spec!r}: unknown target kind {kind!r}") from None
            fetchers[spec] = policy_backend_fetcher(broker, backend, ref, project=rest)
    if len(fetchers) < 2:
        raise ParityTargetError(f"{ref.name}: parity needs at least two targets, got {len(fetchers)}")
    return fetchers


def audit_parity(audit, ref, result: ParityResult) -> None:
    tag = "in_sync" if result.in_sync else ("drift" if len(result.groups) > 1 else "error")
    audit.append("parity", ref.sm_name, tag)
