"""Type a secret into a field of a visible browser tab (PANT-1400, GH #258).

`resolve --exec` and `resolve --stdin` cover commands and files. This covers
"type the card into that checkout page the human is looking at" without the
value passing through the agent: the agent names a reference and points at
fields with CSS selectors, Portunus fetches the value and sends it to the
browser over the DevTools protocol, and the agent gets back only
"typed N characters into <field> on <origin>".

Order of checks, all before the value is fetched:

1. the reference has an origin allow-list (``Reference.allowed_origins``,
   set by a human with ``portunus browser allow``). No list, no typing.
2. the tab is found (exactly one match), and so is every field: each
   selector must match exactly one element across the page and all its
   frames, cross-origin frames included (Stripe's card inputs live in an
   iframe on js.stripe.com).
3. the tab's top-level origin AND the origin of every frame a field lives in
   are on the allow-list. Both, because a card typed into a genuine Stripe
   iframe embedded in an attacker's page still pays the attacker.

Then the value is fetched through the normal resolver, so lifecycle state,
the approval gate and roles apply exactly as for any resolve. Right before
each field is typed, the frame's live ``location.origin`` is checked again
inside the page; after typing it is checked once more and a change is
reported as an error. The audit chain gets one line per field with the
frame origin and the top-level origin.

Structured secrets: ``name#key`` picks one key out of a JSON object value
(the card kind stores ``{"number", "exp_month", "exp_year", "cvc", "name",
"postal"}``). A few derived keys cover how checkout forms split expiry.

Honest limits: the checks run against what the browser reports. A page can
still navigate in the instant between the last origin check and the
keystrokes landing; the post-typing check catches and reports that, it can't
undo it. And a page on an allowed origin receives the value: that is the
point. Keep allow-lists to origins that should get the secret.
"""
from __future__ import annotations

import json
import re
import urllib.parse
from dataclasses import dataclass
from typing import Callable, Dict, List, Optional, Sequence, Tuple

from .cdp import CDPConnection, CDPError, browser_ws_url, endpoint_from


class BrowserFillError(RuntimeError):
    """A fill was refused or failed. The message never contains the value."""


# --- origins -----------------------------------------------------------------
_DEFAULT_PORTS = {"http": 80, "https": 443}


def normalize_origin(raw: str) -> str:
    """`https://Checkout.Stripe.com/pay` -> `https://checkout.stripe.com`.
    Exact origins only: no wildcards, no paths, http(s) only."""
    text = (raw or "").strip()
    if "*" in text:
        raise ValueError(f"wildcards are not allowed in an origin: {raw!r} (list each origin)")
    parsed = urllib.parse.urlsplit(text)
    if parsed.scheme not in _DEFAULT_PORTS or not parsed.hostname:
        raise ValueError(f"not an http(s) origin: {raw!r} (want e.g. https://checkout.stripe.com)")
    host = parsed.hostname.lower()
    if ":" in host:
        host = f"[{host}]"
    port = parsed.port
    if port and port != _DEFAULT_PORTS[parsed.scheme]:
        return f"{parsed.scheme}://{host}:{port}"
    return f"{parsed.scheme}://{host}"


def origin_allowed(origin: str, allowed: Sequence[str]) -> bool:
    try:
        norm = normalize_origin(origin)
    except ValueError:
        return False  # "null", about:, data:, file: -- never allowed
    return norm in {normalize_origin(a) for a in allowed}


# --- structured values ---------------------------------------------------------
_ALIASES = {"postal_code": "postal", "zip": "postal", "zip_code": "postal",
            "card_number": "number", "cvv": "cvc", "csc": "cvc"}


def split_ref(raw: str) -> Tuple[str, str]:
    """`free-reign-card-1#number` -> ("free-reign-card-1", "number")."""
    name, _, key = (raw or "").partition("#")
    return name.strip(), key.strip()


def field_value(raw: str, key: str, ref_name: str = "") -> str:
    """The text to type for `key` of a stored value. No key: the whole value.
    Derived keys for expiry: `expiry` (MM/YY), `exp_mm`, `exp_yy`, `exp_yyyy`.
    Errors name the reference and key, never the value."""
    if not key:
        return raw
    label = f"{ref_name}#{key}" if ref_name else key
    try:
        obj = json.loads(raw)
    except (ValueError, TypeError):
        obj = None
    if not isinstance(obj, dict):
        raise BrowserFillError(f"{label}: the stored value is not a structured (JSON object) secret")
    key = _ALIASES.get(key, key)

    def _int(k: str) -> int:
        try:
            return int(obj[k])
        except (KeyError, TypeError, ValueError):
            raise BrowserFillError(f"{label}: the stored value has no usable {k}") from None

    if key in ("expiry", "exp"):
        return f"{_int('exp_month'):02d}/{_int('exp_year') % 100:02d}"
    if key == "exp_mm":
        return f"{_int('exp_month'):02d}"
    if key == "exp_yy":
        return f"{_int('exp_year') % 100:02d}"
    if key == "exp_yyyy":
        year = _int("exp_year")
        return f"{year + 2000 if year < 100 else year}"
    if key not in obj or isinstance(obj[key], (dict, list)) or obj[key] is None:
        known = ", ".join(sorted(k for k in obj if not isinstance(obj[k], (dict, list))))
        raise BrowserFillError(f"{label}: no such key (keys: {known})")
    return str(obj[key])


# --- field specs ---------------------------------------------------------------
@dataclass
class FieldSpec:
    key: str        # "" for the whole value, else a key of a structured value
    selector: str   # CSS selector, must match exactly one element on the page
    frame: str = "" # optional: only look in frames on this origin


def parse_field_spec(raw: str) -> FieldSpec:
    """`number=input[name=cardnumber]`, `cvc=input[name=cvc]@https://js.stripe.com`,
    or `=#password` (whole value). The frame origin, if any, follows the last
    `@` and must start with http(s)://."""
    key, sep, rest = (raw or "").partition("=")
    if not sep or not rest.strip():
        raise ValueError(f"field spec {raw!r}: want KEY=SELECTOR (KEY may be empty for the whole value)")
    selector, frame = rest, ""
    at = rest.rfind("@")
    if at > 0 and re.match(r"https?://", rest[at + 1:]):
        selector, frame = rest[:at], normalize_origin(rest[at + 1:])
    return FieldSpec(key=key.strip(), selector=selector.strip(), frame=frame)


# --- page inspection ---------------------------------------------------------
@dataclass
class _Frame:
    session: str
    frame_id: str
    context_id: int
    origin: str


@dataclass
class _Located:
    spec: FieldSpec
    frame: _Frame


_COUNT_FN = "function(sel){try{return document.querySelectorAll(sel).length}catch(e){return -1}}"
_FOCUS_FN = (
    "function(sel, origin){"
    "if (location.origin !== origin) return 'origin-changed:' + location.origin;"
    "var els = document.querySelectorAll(sel); if (els.length !== 1) return 'count:' + els.length;"
    "var el = els[0]; el.focus();"
    "if (typeof el.select === 'function') { try { el.select(); } catch (e) {} }"
    "else if (el.isContentEditable) { var r = document.createRange(); r.selectNodeContents(el);"
    "var s = getSelection(); s.removeAllRanges(); s.addRange(r); }"
    "return document.activeElement === el ? 'ok' : 'not-focused'; }"
)
# Length only, letters and digits only (checkout fields insert spaces and
# slashes as they format). Never the value.
_LEN_FN = (
    "function(sel){var els = document.querySelectorAll(sel); if (els.length !== 1) return -1;"
    "var el = els[0]; var v = ('value' in el) ? String(el.value) : String(el.textContent || '');"
    "return v.replace(/[^0-9A-Za-z]/g, '').length; }"
)
_SET_FN = (
    "function(sel, origin, v){"
    "if (location.origin !== origin) return 'origin-changed:' + location.origin;"
    "var els = document.querySelectorAll(sel); if (els.length !== 1) return 'count:' + els.length;"
    "var el = els[0]; var proto = el instanceof HTMLTextAreaElement ? HTMLTextAreaElement.prototype"
    " : el instanceof HTMLSelectElement ? HTMLSelectElement.prototype : HTMLInputElement.prototype;"
    "var d = Object.getOwnPropertyDescriptor(proto, 'value'); if (!d || !d.set) return 'no-setter';"
    "d.set.call(el, v); el.dispatchEvent(new Event('input', {bubbles: true}));"
    "el.dispatchEvent(new Event('change', {bubbles: true})); return 'ok'; }"
)


def _alnum_len(text: str) -> int:
    return len(re.sub(r"[^0-9A-Za-z]", "", text))


class _Page:
    """One attached tab: its session, its OOPIF sessions, and an isolated
    JS world in every frame (page scripts can't see or patch our functions)."""

    def __init__(self, conn: CDPConnection, target_id: str):
        self.conn = conn
        self.session = conn.send("Target.attachToTarget", {"targetId": target_id, "flatten": True})["sessionId"]
        self.sessions = [self.session]
        self._auto_attach(self.session)
        self.frames: List[_Frame] = []
        for sid in self.sessions:
            self.frames.extend(self._frames_of(sid))

    def _auto_attach(self, session: str) -> None:
        """Attach to out-of-process (cross-site) iframes, recursively."""
        seen = len(self.conn.events)
        self.conn.send("Target.setAutoAttach", {"autoAttach": True, "waitForDebuggerOnStart": False,
                                                "flatten": True}, session_id=session)
        self.conn.drain()
        for ev in self.conn.events[seen:]:
            if ev.get("method") != "Target.attachedToTarget":
                continue
            params = ev.get("params", {})
            sid = params.get("sessionId", "")
            if params.get("targetInfo", {}).get("type") == "iframe" and sid and sid not in self.sessions:
                self.sessions.append(sid)
                self._auto_attach(sid)

    def _frames_of(self, session: str) -> List[_Frame]:
        try:
            tree = self.conn.send("Page.getFrameTree", session_id=session)["frameTree"]
        except CDPError:
            return []
        frames: List[_Frame] = []
        stack = [tree]
        while stack:
            node = stack.pop()
            stack.extend(node.get("childFrames", []))
            frame_id = node["frame"]["id"]
            try:
                ctx = self.conn.send("Page.createIsolatedWorld",
                                     {"frameId": frame_id, "worldName": "portunus-fill"},
                                     session_id=session)["executionContextId"]
                origin = self._eval(session, ctx, "location.origin")
            except CDPError:
                continue  # a frame whose document lives in another process/session
            frames.append(_Frame(session, frame_id, ctx, str(origin)))
        return frames

    def _eval(self, session: str, ctx: int, expression: str):
        res = self.conn.send("Runtime.evaluate", {"expression": expression, "contextId": ctx,
                                                  "returnByValue": True}, session_id=session)
        if "exceptionDetails" in res:
            raise CDPError("script error in page")
        return res.get("result", {}).get("value")

    def call(self, frame: _Frame, fn: str, *args) -> object:
        """Call `fn` in `frame`'s isolated world. Arguments travel as CDP
        call arguments, never spliced into source; errors never echo them."""
        glob = self.conn.send("Runtime.evaluate", {"expression": "this", "contextId": frame.context_id},
                              session_id=frame.session)["result"]["objectId"]
        res = self.conn.send("Runtime.callFunctionOn", {
            "functionDeclaration": fn, "objectId": glob, "returnByValue": True, "userGesture": True,
            "arguments": [{"value": a} for a in args],
        }, session_id=frame.session)
        if "exceptionDetails" in res:
            raise CDPError("script error in page")
        return res.get("result", {}).get("value")

    def top_origin(self) -> str:
        main = next((f for f in self.frames if f.session == self.session), None)
        if main is None:
            raise BrowserFillError("could not read the tab's top-level origin")
        # The first frame popped from the main session's tree is the main frame.
        return main.origin

    def locate(self, spec: FieldSpec) -> _Located:
        hits: List[Tuple[_Frame, int]] = []
        for fr in self.frames:
            if spec.frame and not origin_allowed(fr.origin, [spec.frame]):
                continue
            try:
                n = int(self.call(fr, _COUNT_FN, spec.selector) or 0)
            except CDPError:
                continue
            if n < 0:
                raise BrowserFillError(f"invalid CSS selector: {spec.selector!r}")
            if n:
                hits.append((fr, n))
        total = sum(n for _, n in hits)
        where = f" in frames on {spec.frame}" if spec.frame else ""
        if total == 0:
            raise BrowserFillError(f"no element matches {spec.selector!r}{where}")
        if total > 1:
            origins = sorted({fr.origin for fr, _ in hits})
            raise BrowserFillError(
                f"{spec.selector!r} matches {total} elements{where} (frames: {', '.join(origins)}) -- "
                f"use a more specific selector, or add @<origin> to pick the frame"
            )
        return _Located(spec, hits[0][0])

    def type_into(self, loc: _Located, text: str) -> str:
        """Focus the field, insert `text`, verify by length. Returns the
        method that worked ("keyboard" or "setter")."""
        fr, sel = loc.frame, loc.spec.selector
        state = self.call(fr, _FOCUS_FN, sel, fr.origin)
        if isinstance(state, str) and state.startswith("origin-changed"):
            raise BrowserFillError(f"the frame for {sel!r} left {fr.origin} before typing -- nothing typed")
        want = _alnum_len(text)
        if state == "ok":
            sent = False
            for session in dict.fromkeys([fr.session, self.session]):
                try:
                    self.conn.send("Input.insertText", {"text": text}, session_id=session)
                    sent = True
                    break
                except CDPError:
                    continue
            if sent and self._check_after(fr, sel) == want:
                return "keyboard"
        # Keyboard path unavailable or the field did not take it all: set the
        # value the way frameworks expect (native setter + input/change).
        state = self.call(fr, _SET_FN, sel, fr.origin, text)
        if isinstance(state, str) and state.startswith("origin-changed"):
            raise BrowserFillError(f"the frame for {sel!r} left {fr.origin} before typing")
        got = self._check_after(fr, sel)
        if state != "ok" or got != want:
            raise BrowserFillError(f"{sel!r} on {fr.origin} did not accept the input "
                                   f"(field shows {got} of {want} characters)")
        return "setter"

    def _check_after(self, fr: _Frame, sel: str) -> int:
        try:
            origin = self.call(fr, "function(){return location.origin}")
        except CDPError:
            origin = None
        if origin != fr.origin:
            raise BrowserFillError(
                f"the frame for {sel!r} navigated away from {fr.origin} while typing "
                f"(now {origin or 'unknown'}) -- treat the value as possibly exposed there"
            )
        return int(self.call(fr, _LEN_FN, sel) or 0)


def _pick_tab(conn: CDPConnection, tab: str) -> dict:
    pages = [t for t in conn.send("Target.getTargets")["targetInfos"] if t.get("type") == "page"]
    if not pages:
        raise BrowserFillError("the browser has no open tabs")
    if tab:
        exact = [t for t in pages if t.get("targetId") == tab]
        pages = exact or [t for t in pages if tab in t.get("url", "") or tab in t.get("title", "")]
    if len(pages) == 1:
        return pages[0]
    listing = "; ".join(f"{t.get('targetId', '')[:8]} {t.get('url', '')}" for t in pages[:10])
    if not pages:
        raise BrowserFillError(f"no tab matches {tab!r}")
    raise BrowserFillError(f"{len(pages)} tabs match {tab or '(any)'!r} -- pass --tab with a URL "
                           f"fragment, title or target id: {listing}")


# --- the fill -------------------------------------------------------------------
@dataclass
class FilledField:
    key: str
    selector: str
    origin: str
    chars: int
    method: str

    def to_dict(self) -> dict:
        return {"key": self.key, "selector": self.selector, "origin": self.origin,
                "typed": self.chars, "method": self.method}


def fill(
    resolver,
    name: str,
    fields: Sequence[FieldSpec],
    tab: str = "",
    cdp: str = "",
    connect: Optional[Callable[[str], CDPConnection]] = None,
) -> Dict[str, object]:
    """Type reference `name` (or keys of it) into `fields` of a browser tab.

    Returns {"name", "tab_origin", "fields": [{key, selector, origin, typed,
    method}]} -- never a value. Raises BrowserFillError (refused or failed),
    or the resolver's own NotInjectable / ApprovalRequired / NotAuthorized /
    UnknownReference / BackendError, all before anything is typed.
    `connect` is a test seam: endpoint -> CDPConnection.
    """
    registry, audit = resolver.registry, resolver.broker.audit
    if not fields:
        raise BrowserFillError("at least one field is required")
    ref = registry.get(name)
    if ref is None:
        from .resolver import UnknownReference
        raise UnknownReference(name)
    allowed = list(ref.allowed_origins or [])
    if not allowed:
        audit.append("browser_fill", ref.sm_name, "denied-no-allowed-origins")
        raise BrowserFillError(
            f"{name} has no allowed origins, so it is never typed into a page -- a human can "
            f"allow one with: portunus browser allow {name} https://<origin>"
        )

    endpoint = endpoint_from(cdp)
    conn = connect(endpoint) if connect else CDPConnection(browser_ws_url(endpoint))
    try:
        target = _pick_tab(conn, tab)
        page = _Page(conn, target["targetId"])
        top = page.top_origin()
        located = [page.locate(spec) for spec in fields]
        refused = sorted({o for o in [top] + [l.frame.origin for l in located] if not origin_allowed(o, allowed)})
        if refused:
            audit.append("browser_fill", ref.sm_name, f"denied-origin:{','.join(refused)}")
            raise BrowserFillError(
                f"{name} may not be typed into {', '.join(refused)} (allowed: {', '.join(allowed)}). "
                f"If this page should get it, a human can run: portunus browser allow {name} <origin>"
            )

        filled: List[FilledField] = []

        def boundary(raw: str) -> None:
            # Every key is extracted before anything is typed, so a missing
            # key never leaves a half-filled form.
            texts = [field_value(raw, l.spec.key, name) for l in located]
            for loc, text in zip(located, texts):
                desc = f"{loc.frame.origin}#{loc.spec.key or 'value'}(top={top})"
                try:
                    method = page.type_into(loc, text)
                except (BrowserFillError, CDPError) as exc:
                    audit.append("browser_fill", ref.sm_name, f"error:{desc}")
                    raise BrowserFillError(_scrub(str(exc), texts)) from None
                audit.append("browser_fill", ref.sm_name, f"ok:{desc}")
                filled.append(FilledField(loc.spec.key, loc.spec.selector, loc.frame.origin,
                                          len(text), method))
            del texts

        resolver.resolve_call(f"{{{{secret:{name}}}}}", boundary)
        return {"name": name, "tab_origin": top, "fields": [f.to_dict() for f in filled]}
    except CDPError as exc:
        raise BrowserFillError(str(exc)) from None
    finally:
        conn.close()


def _scrub(message: str, secrets: Sequence[str]) -> str:
    for s in secrets:
        if s and len(s) >= 3:
            message = message.replace(s, "[redacted]")
    return message
