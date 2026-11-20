"""Inbox password reset -- set a web account's password through the
account's own inbox (PANT-1402, GH #256).

A person creates an account for an agent (e.g. the GitHub machine account
``skewbald-io`` on ``skewbald@agentmail.to``) with a throwaway password and
means the agent to replace it. ``portunus reset <name>`` does that without a
person or a visible browser:

  1. preflight -- the reference is tagged for a reset, the inbox answers, and
                  the provider's reset page shows no human check (read-only)
  2. request   -- ask the provider for a reset email for the account's address
  3. link      -- read the reset link from the inbox through the inbox's own
                  API key (resolved inside ``Resolver.resolve_call()``)
  4. stash     -- the current password (if any) is copied to
                  ``<sm_name>.previous``; a ``<sm_name>.resetting`` marker is set
  5. store     -- a new strong password is generated and stored as the
                  reference's value BEFORE anything is submitted
  6. submit    -- the reset form is filled with the value read back from the
                  vault and posted, headless (plain HTTP, no browser window)
  7. record    -- marker cleared, audit entry (who/when), rotation binding
                  ``<provider>-password`` and ``password_reset_*`` tags updated

A captcha or any other human check stops the flow with `HumanCheckRequired`
before the password changes; whatever was stashed is put back.

Reference tags (all metadata, never a value):
  ``reset_provider``   recipe name (``github``); defaults to the ref's provider
  ``reset_email``      the account's email address -- an inbox Portunus reads
  ``reset_inbox``      inbox kind, ``agentmail`` (the default)
  ``reset_inbox_ref``  reference holding the inbox's API key

Secret-boundary invariant: the reset link and the new password are held in
locals only. They reach the local vault, an outbound form body, or nothing.
No step, report, audit entry or error carries either, nor a page body or an
email body.
"""
from __future__ import annotations

import html as html_mod
import http.cookiejar
import os
import re
import secrets
import time
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass, field
from datetime import datetime, timezone
from html.parser import HTMLParser
from typing import Callable, Dict, List, Optional, Tuple

from .rotation import RotationAdapterError, RotationBinding, RotationResult, _json_http, update_rotation_bindings

PROVIDER_TAG = "reset_provider"
EMAIL_TAG = "reset_email"
INBOX_TAG = "reset_inbox"
INBOX_REF_TAG = "reset_inbox_ref"
LAST_AT_TAG = "password_reset_at"
LAST_BY_TAG = "password_reset_by"
PREVIOUS_SUFFIX = ".previous"
# Present from the moment the new password is stored until the provider is
# known to have accepted (or refused) it. A reset refuses to start over it:
# the vault may hold a password the provider never took, and the previous
# one is the only copy of what might still be live.
RESETTING_SUFFIX = ".resetting"
_PASSWORD_BYTES = 24  # token_urlsafe(24) -> 32 chars from [A-Za-z0-9_-]
# Accept a reset email up to this many seconds older than the request:
# absorbs clock skew between this host and the inbox's timestamps.
_EMAIL_MARGIN = 120
_USER_AGENT = "Mozilla/5.0 (X11; Linux x86_64) portunus-reset"


class ResetError(RotationAdapterError):
    """A reset step failed. Never carries a value, a link or a page body."""


class HumanCheckRequired(ResetError):
    """The provider wants a person (captcha, device check, 2FA). The flow
    stops here; a person finishes the reset by hand."""


# --- provider recipes ----------------------------------------------------------


@dataclass(frozen=True)
class ResetRecipe:
    """Everything provider-specific about a reset, as data."""

    name: str
    request_url: str
    # Substring of the request form's action (picks the right <form>).
    request_action: str
    email_field: str
    # Reset email: sender contains this, subject contains this (case-insensitive).
    sender: str
    subject: str
    link_pattern: str
    # Substring of the reset form's action on the link's page.
    submit_action: str
    # Shown in the plan before anything runs.
    revocation_note: str
    # Final-URL substrings after submit that mean "password accepted, but
    # signing in needs a person" (2FA / device verification).
    accepted_needs_person: Tuple[str, ...] = ()
    # Final-URL substrings after submit that mean the reset was refused.
    refused_urls: Tuple[str, ...] = ()
    # Page text that means the request/submit was refused.
    error_markers: Tuple[str, ...] = ()


GITHUB = ResetRecipe(
    name="github",
    request_url="https://github.com/password_reset",
    request_action="/password_reset",
    email_field="email",
    sender="github.com",
    subject="reset your password",
    link_pattern=r"https://github\.com/password_reset/[A-Za-z0-9_\-]+",
    submit_action="/password_reset",
    revocation_note=(
        "GitHub's docs do not say whether a password reset revokes personal access tokens, "
        "OAuth app tokens or SSH keys; web sessions may be signed out. After the reset, check "
        "the account's API token still works (`portunus liveness check <token-ref>`)."),
    accepted_needs_person=("/sessions/two-factor", "/sessions/verified-device"),
    refused_urls=("/password_reset/",),
    error_markers=("flash-error", "Password is too short", "Password is weak",
                   "reset link has expired", "invalid or expired"),
)

RECIPES: Dict[str, ResetRecipe] = {GITHUB.name: GITHUB}


# --- inbox readers ---------------------------------------------------------------

Transport = Callable[[str, str, Dict[str, str], Optional[dict], float], Tuple[int, dict]]


def _agentmail_transport(method, url, headers, body, timeout):
    return _json_http(method, url, headers, body, timeout, "AgentMail API")


def _iso(ts: float) -> str:
    return datetime.fromtimestamp(ts, timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _ts(value) -> float:
    try:
        return datetime.fromisoformat(str(value).replace("Z", "+00:00")).timestamp()
    except ValueError:
        return 0.0


class AgentMailInbox:
    """Reads an AgentMail inbox (https://docs.agentmail.to) with the inbox's
    API key, resolved only inside `resolver.resolve_call`. The inbox id is
    the inbox's email address."""

    API = "https://api.agentmail.to/v0"

    def __init__(self, resolver, key_ref: str, address: str, transport: Optional[Transport] = None,
                 timeout: float = 30.0):
        self._resolver = resolver
        self._key = "{{secret:%s}}" % key_ref
        self._address = address
        self._transport = transport or _agentmail_transport
        self._timeout = timeout

    def _call(self, path: str, query: Optional[dict] = None) -> Tuple[int, dict]:
        url = f"{self.API}/inboxes/{urllib.parse.quote(self._address, safe='')}{path}"
        if query:
            url += "?" + urllib.parse.urlencode(query)

        def boundary(key: str):
            return self._transport("GET", url, {"Authorization": f"Bearer {key}",
                                                "User-Agent": "portunus-reset"}, None, self._timeout)

        return self._resolver.resolve_call(self._key, boundary)

    def check(self) -> str:
        status, _ = self._call("/messages", {"limit": 1})
        if status != 200:
            raise ResetError(f"AgentMail inbox {self._address} not readable (HTTP {status})")
        return f"AgentMail inbox {self._address} readable"

    def find(self, since: float, sender: str, subject: str, pattern: str) -> str:
        """The first match of `pattern` in a message newer than `since` from
        `sender` with `subject`, or "" if none has arrived yet. The match is a
        credential (a reset link): the caller must not print it."""
        status, resp = self._call("/messages", {"after": _iso(since - _EMAIL_MARGIN), "limit": 20})
        if status != 200:
            raise ResetError(f"AgentMail message list failed (HTTP {status})")
        rx = re.compile(pattern)
        items = [m for m in (resp.get("messages") or []) if isinstance(m, dict)]
        del resp
        items.sort(key=lambda m: _ts(m.get("timestamp") or m.get("created_at")), reverse=True)
        for item in items:
            if _ts(item.get("timestamp") or item.get("created_at")) < since - _EMAIL_MARGIN:
                continue
            if sender.lower() not in str(item.get("from", "")).lower():
                continue
            if subject.lower() not in str(item.get("subject", "")).lower():
                continue
            mid = str(item.get("message_id", ""))
            if not mid:
                continue
            status, msg = self._call("/messages/" + urllib.parse.quote(mid, safe=""))
            if status != 200:
                continue
            for part in ("text", "html", "extracted_text", "extracted_html"):
                body = msg.get(part)
                if isinstance(body, str):
                    found = rx.search(html_mod.unescape(body))
                    if found:
                        return found.group(0)
        return ""


INBOXES = {"agentmail": AgentMailInbox}


# --- headless form client ----------------------------------------------------------

_HUMAN_CHECK_RX = re.compile(r"captcha|arkose|funcaptcha|turnstile|challenge-platform", re.I)


@dataclass
class Form:
    action: str
    method: str
    inputs: List[Dict[str, str]] = field(default_factory=list)
    human_check: bool = False

    def fields(self) -> Dict[str, str]:
        """Hidden and pre-filled fields to send back as they are."""
        out = {}
        for i in self.inputs:
            if i.get("name") and i.get("type", "text") not in ("password", "submit", "button",
                                                                 "checkbox", "radio", "file"):
                out[i["name"]] = i.get("value", "")
        return out

    def password_fields(self) -> List[str]:
        return [i["name"] for i in self.inputs if i.get("type") == "password" and i.get("name")]


class _FormParser(HTMLParser):
    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.forms: List[Form] = []
        self._cur: Optional[Form] = None
        self.page_human_check = False

    def handle_starttag(self, tag, attrs):
        a = {k: (v or "") for k, v in attrs}
        blob = " ".join(a.values())
        if tag == "form":
            self._cur = Form(action=a.get("action", ""), method=a.get("method", "get").lower())
            self.forms.append(self._cur)
            return
        hit = bool(_HUMAN_CHECK_RX.search(blob)) and tag in ("iframe", "div", "input", "script",
                                                            "textarea")
        if self._cur is None:
            if hit and tag in ("iframe", "div"):
                self.page_human_check = True
            return
        if hit:
            self._cur.human_check = True
        if tag in ("input", "textarea", "button") and a.get("name"):
            self._cur.inputs.append({"name": a["name"], "type": a.get("type", "text").lower(),
                                     "value": a.get("value", "")})

    def handle_endtag(self, tag):
        if tag == "form":
            self._cur = None


def parse_forms(page: str) -> Tuple[List[Form], bool]:
    p = _FormParser()
    p.feed(page)
    return p.forms, p.page_human_check


@dataclass(frozen=True)
class Page:
    status: int
    url: str
    text: str


class HttpSession:
    """A cookie-keeping HTTP client -- the "headless browser" for plain
    HTML form flows. Nothing renders, so nothing can show a value."""

    def __init__(self, timeout: float = 30.0):
        self._jar = http.cookiejar.CookieJar()
        self._opener = urllib.request.build_opener(urllib.request.HTTPCookieProcessor(self._jar))
        self._timeout = timeout

    def request(self, method: str, url: str, form: Optional[Dict[str, str]] = None,
                referer: str = "") -> Page:
        data = urllib.parse.urlencode(form).encode() if form is not None else None
        headers = {"User-Agent": _USER_AGENT, "Accept": "text/html"}
        if data is not None:
            headers["Content-Type"] = "application/x-www-form-urlencoded"
        if referer:
            headers["Referer"] = referer
        req = urllib.request.Request(url, data=data, headers=headers, method=method)
        try:
            with self._opener.open(req, timeout=self._timeout) as resp:
                return Page(resp.status, resp.geturl(), resp.read().decode("utf-8", "replace"))
        except urllib.error.HTTPError as exc:
            return Page(exc.code, exc.geturl() or url, exc.read().decode("utf-8", "replace"))
        except urllib.error.URLError as exc:
            raise ResetError(f"{urllib.parse.urlsplit(url).netloc} unreachable: {exc.reason}") from None


# --- the flow ----------------------------------------------------------------------


@dataclass(frozen=True)
class Step:
    name: str
    detail: str


@dataclass(frozen=True)
class ResetReport:
    """Outcome of a reset. Structurally incapable of carrying a value."""

    ref_name: str
    provider: str
    dry_run: bool
    steps: Tuple[Step, ...] = ()
    warnings: Tuple[str, ...] = ()
    needs_person: str = ""
    timestamp: int = field(default_factory=lambda: int(time.time()))


def _safe_url(url: str) -> str:
    """Scheme + host only -- a reset URL's path is the credential."""
    parts = urllib.parse.urlsplit(url)
    return f"{parts.scheme}://{parts.netloc}"


def recipe_for(ref) -> ResetRecipe:
    tags = getattr(ref, "tags", None) or {}
    name = tags.get(PROVIDER_TAG) or getattr(ref, "provider", "") or ""
    recipe = RECIPES.get(name)
    if recipe is None:
        raise ResetError(f"{ref.name!r}: no reset recipe for provider {name!r} -- set the "
                         f"{PROVIDER_TAG!r} tag to one of: {', '.join(sorted(RECIPES))}")
    return recipe


class InboxPasswordReset:
    """Drives one reset. Every network call goes through `session_factory`
    (provider pages) or the inbox reader, so tests never touch a real site."""

    def __init__(self, resolver, registry=None, local_backend=None, audit=None,
                 session_factory: Optional[Callable[[], HttpSession]] = None,
                 inbox_factory: Optional[Callable[..., object]] = None,
                 clock: Callable[[], float] = time.time,
                 sleep: Callable[[float], None] = time.sleep,
                 email_wait: float = 180.0, poll_interval: float = 5.0,
                 actor: Optional[str] = None, bindings_path=None):
        self._resolver = resolver
        self._registry = registry if registry is not None else getattr(resolver, "registry", None)
        if local_backend is None:
            from .localvault import LocalEncryptedBackend
            local_backend = LocalEncryptedBackend()
        self._local = local_backend
        self._audit = audit
        self._session_factory = session_factory or HttpSession
        self._inbox_factory = inbox_factory
        self._clock = clock
        self._sleep = sleep
        self._email_wait = email_wait
        self._poll = poll_interval
        self._actor = actor or os.environ.get("DOSTAL_AGENT") or os.environ.get("USER", "unknown")
        self._bindings_path = bindings_path
        self._needs_person = ""

    # -- public --------------------------------------------------------------

    def reset(self, ref, dry_run: bool = False, force: bool = False) -> ResetReport:
        recipe = recipe_for(ref)
        name, sm_name = ref.name, ref.sm_name
        tags = dict(ref.tags or {})
        email = tags.get(EMAIL_TAG, "")
        if not email or "@" not in email:
            raise ResetError(f"{name!r} has no {EMAIL_TAG!r} tag -- the account's email, an inbox "
                             "Portunus can read")
        if getattr(ref, "backend", "") not in ("", "local"):
            raise ResetError(f"{name!r} lives in the {ref.backend!r} backend; a reset stores the new "
                             "password in the local vault only")
        marker, prev_name = sm_name + RESETTING_SUFFIX, sm_name + PREVIOUS_SUFFIX
        if self._local.has(marker) and not force:
            raise ResetError(
                f"an earlier reset of {name!r} did not finish -- the vault holds a password "
                f"{recipe.name} may not have taken, and {prev_name} the one before it. Sign in with "
                f"the stored value; once you know which is live, re-run with --force")
        inbox = self._inbox(ref, tags, email)

        steps = [Step("preflight", inbox.check())]
        session = self._session_factory()
        page = session.request("GET", recipe.request_url)
        form = self._pick_form(page, recipe.request_action, recipe, "reset request page")
        steps.append(Step("preflight", f"{_safe_url(recipe.request_url)} reset form found, no human check"))
        warnings = (recipe.revocation_note,)
        if dry_run:
            steps += [Step("request", f"ask {recipe.name} for a reset email to {email}"),
                      Step("link", f"wait up to {int(self._email_wait)}s for it in {email}"),
                      Step("store", f"new password stored as {sm_name} before submitting "
                                    f"(current kept as {prev_name})"),
                      Step("submit", "post the reset form headless"),
                      Step("record", f"audit + rotation binding {recipe.name}-password")]
            return ResetReport(name, recipe.name, True, tuple(steps), warnings)

        # 2. request
        start = self._clock()
        fields = form.fields()
        fields[recipe.email_field] = email
        resp = session.request("POST", urllib.parse.urljoin(page.url, form.action), fields, referer=page.url)
        self._check_page(resp, recipe, "reset request")
        steps.append(Step("request", f"reset email requested for {email} (HTTP {resp.status})"))
        self._audit_append(name, f"ok:requested:{recipe.name}")

        # 3. link
        link = self._wait_for_link(inbox, start, recipe)
        steps.append(Step("link", f"reset link read from {email}"))

        # 4-6. stash, store, submit
        try:
            steps += self._store_and_submit(ref, recipe, link, marker, prev_name)
        finally:
            del link
        steps.append(Step("record", self._record(ref, recipe)))
        return ResetReport(name, recipe.name, False, tuple(steps), warnings,
                           needs_person=self._needs_person)

    # -- steps ---------------------------------------------------------------

    def _inbox(self, ref, tags, email):
        kind = tags.get(INBOX_TAG, "agentmail")
        key_ref = tags.get(INBOX_REF_TAG, "")
        if not key_ref:
            raise ResetError(f"{ref.name!r} has no {INBOX_REF_TAG!r} tag -- the reference holding "
                             f"the {kind} API key for {email}")
        if self._inbox_factory is not None:
            return self._inbox_factory(kind, self._resolver, key_ref, email)
        cls = INBOXES.get(kind)
        if cls is None:
            raise ResetError(f"no inbox reader {kind!r}; known: {', '.join(sorted(INBOXES))}")
        return cls(self._resolver, key_ref, email)

    def _pick_form(self, page: Page, action: str, recipe: ResetRecipe, what: str) -> Form:
        if page.status >= 400:
            raise ResetError(f"{recipe.name} {what} answered HTTP {page.status}")
        forms, page_check = parse_forms(page.text)
        matches = [f for f in forms if action in f.action]
        if any(f.human_check for f in matches) or (page_check and matches):
            raise HumanCheckRequired(
                f"{recipe.name} shows a captcha or other human check on the {what}; a person has to "
                "finish this reset by hand")
        if not matches:
            raise ResetError(f"{recipe.name} {what}: no form posting to {action!r} -- the page "
                             "changed or wants a person")
        return matches[0]

    def _check_page(self, page: Page, recipe: ResetRecipe, what: str) -> None:
        if page.status >= 400:
            raise ResetError(f"{recipe.name} {what} answered HTTP {page.status}")
        forms, page_check = parse_forms(page.text)
        if page_check or any(f.human_check for f in forms):
            raise HumanCheckRequired(f"{recipe.name} asked for a human check after the {what}; a "
                                     "person has to finish this reset by hand")

    def _wait_for_link(self, inbox, start: float, recipe: ResetRecipe) -> str:
        deadline = start + self._email_wait
        while True:
            link = inbox.find(start, recipe.sender, recipe.subject, recipe.link_pattern)
            if link:
                return link
            if self._clock() >= deadline:
                raise ResetError(f"no {recipe.name} reset email arrived within "
                                 f"{int(self._email_wait)}s -- check the {EMAIL_TAG!r} tag is the "
                                 "account's email")
            self._sleep(self._poll)

    def _store_and_submit(self, ref, recipe, link, marker, prev_name) -> List[Step]:
        sm_name, steps = ref.sm_name, []
        self._needs_person = ""
        session = self._session_factory()
        page = session.request("GET", link)
        # A human check here means nothing changed yet -- bail before storing.
        form = self._pick_form(page, recipe.submit_action, recipe, "reset link page")
        pw_fields = form.password_fields()
        if not pw_fields:
            raise ResetError(f"{recipe.name} reset link page has no password field -- the link may "
                             "have expired")

        had_previous = self._local.has(sm_name)
        self._local.store(marker, recipe.name)
        if had_previous:
            current = self._local.access(sm_name)
            try:
                self._local.store(prev_name, current)
            finally:
                del current
            steps.append(Step("stash", f"current password kept as {prev_name} (local vault)"))
        new_value = secrets.token_urlsafe(_PASSWORD_BYTES)
        try:
            self._local.store(sm_name, new_value)
        finally:
            del new_value
        steps.append(Step("store", f"new password stored as {sm_name} (local vault)"))

        fields = form.fields()
        sent = False
        try:
            stored = self._local.access(sm_name)
            try:
                for f in pw_fields:
                    fields[f] = stored
                sent = True
                resp = session.request("POST", urllib.parse.urljoin(page.url, form.action), fields,
                                       referer=_safe_url(page.url))
            finally:
                del stored
                fields.clear()
        except ResetError as exc:
            if not sent:
                self._restore(ref, had_previous, prev_name, marker)
                raise
            self._audit_append(ref.name, f"err:submit-unknown:{recipe.name}")
            raise ResetError(f"{exc} -- the reset form was sent but no answer came back; the new "
                             f"password is stored as {sm_name}, the previous one as {prev_name}. "
                             f"Sign in to see which is live, then re-run with --force") from None

        final = urllib.parse.urlsplit(resp.url).path
        if any(u in final for u in recipe.accepted_needs_person):
            self._needs_person = (f"{recipe.name} took the new password but wants a person to finish "
                                  "signing in (two-factor or device check)")
        elif resp.status >= 400 or any(u in final for u in recipe.refused_urls) or \
                any(m in resp.text for m in recipe.error_markers):
            self._restore(ref, had_previous, prev_name, marker)
            self._audit_append(ref.name, f"err:refused:{recipe.name}")
            raise ResetError(f"{recipe.name} refused the new password (HTTP {resp.status}); the "
                             "previous value is back in place")
        self._local.remove(marker)
        steps.append(Step("submit", f"{recipe.name} accepted the new password (HTTP {resp.status})"))
        return steps

    def _restore(self, ref, had_previous, prev_name, marker) -> None:
        if had_previous:
            value = self._local.access(prev_name)
            try:
                self._local.store(ref.sm_name, value)
            finally:
                del value
            self._local.remove(prev_name)
        else:
            self._local.remove(ref.sm_name)
        self._local.remove(marker)

    def _record(self, ref, recipe) -> str:
        now = self._clock()
        self._audit_append(ref.name, f"ok:reset:{recipe.name}")
        provider = f"{recipe.name}-password"
        email = (ref.tags or {}).get(EMAIL_TAG, "")

        def mutate(bindings):
            bindings[provider] = RotationBinding(provider=provider, status="real", account=email)

        if self._bindings_path is not None:
            update_rotation_bindings(mutate, self._bindings_path)
        else:
            update_rotation_bindings(mutate)
        if self._registry is not None and hasattr(self._registry, "retag"):
            tags = dict(ref.tags or {})
            tags[LAST_AT_TAG] = _iso(now)
            tags[LAST_BY_TAG] = self._actor
            # The new value lives in the local vault: pin an unpinned ref there so it
            # resolves from where it was written (same rule as prompt/drop, PANT-1218).
            pin = {"backend": "local"} if not getattr(ref, "backend", "") else {}
            self._registry.retag(ref.name, tags=tags, **pin)
        return f"rotation binding {provider} updated; reset by {self._actor} at {_iso(now)}"

    def _audit_append(self, name: str, result: str) -> None:
        if self._audit is not None:
            self._audit.append("reset", name, result, actor=self._actor)


class InboxResetRotationAdapter:
    """`portunus rotation run` entry point for ``<recipe>-password``
    providers: runs the reset non-interactively."""

    def __init__(self, **kw):
        self._kw = kw

    def capability(self) -> str:
        return "auto"

    def rotate(self, ref, resolver=None, retire_old: bool = False) -> RotationResult:
        if resolver is None:
            raise RotationAdapterError(f"resetting {ref.name!r} needs a resolver")
        report = InboxPasswordReset(resolver, **self._kw).reset(ref)
        return RotationResult(provider=f"{report.provider}-password", ref_name=ref.name,
                              phase="reset", retired_old=False)
