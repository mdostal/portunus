"""Inbox password reset tests (PANT-1402, GH #256).

A fake GitHub (the reset request page, the reset email, the link page) and a
fake AgentMail API share one timeline, so the tests can assert the ORDER of
every step -- above all that the new password is in the vault before the
reset form is posted. Nothing contacts a real site; every value is fake."""
import json
from datetime import datetime, timezone

import pytest

from portunus import inbox_reset as mod
from portunus.audit import AuditChain
from portunus.inbox_reset import (
    AgentMailInbox,
    HumanCheckRequired,
    InboxPasswordReset,
    Page,
    ResetError,
    parse_forms,
)
from portunus.registry import Registry
from portunus.rotation import load_rotation_bindings, rotation_adapter_for

INBOX_KEY = "am_FAKE_INBOX_KEY_aaaaaaaaaaaaaaaaaaaa"
OLD_PW = "FAKE_TEMP_PASSWORD_bbbbbbbb"
TOKEN = "FAKEresetTOKENcccccccccccccc"
LINK = f"https://github.com/password_reset/{TOKEN}"
EMAIL = "skewbald@agentmail.to"
REF = "skewbald-io-github-password"

REQUEST_PAGE = """<html><body>
<form action="/search" method="get"><input name="q"></form>
<form action="/password_reset" method="post">
  <input type="hidden" name="authenticity_token" value="tok-1">
  <input type="text" name="email">
  <input type="submit" name="commit" value="Send">
</form></body></html>"""

CAPTCHA_PAGE = """<html><body>
<form action="/password_reset" method="post">
  <input type="hidden" name="authenticity_token" value="tok-1">
  <input type="text" name="email">
  <div class="octocaptcha-frame"><iframe src="https://octocaptcha.com/x"></iframe></div>
</form></body></html>"""

LINK_PAGE = f"""<html><body>
<form action="/password_reset/{TOKEN}" method="post">
  <input type="hidden" name="authenticity_token" value="tok-2">
  <input type="hidden" name="_method" value="put">
  <input type="password" name="password">
  <input type="password" name="password_confirmation">
</form></body></html>"""


class _Clock:
    def __init__(self):
        self.t = 1_800_000_000.0

    def __call__(self):
        return self.t

    def sleep(self, s):
        self.t += s

    def iso(self, offset=0.0):
        return datetime.fromtimestamp(self.t + offset, timezone.utc).isoformat().replace("+00:00", "Z")


class _Local:
    """In-memory stand-in for LocalEncryptedBackend."""

    def __init__(self, timeline, data=None):
        self.data = dict(data or {})
        self.timeline = timeline

    def store(self, name, value):
        self.timeline.append(("store", name))
        self.data[name] = value

    def access(self, name, project=""):
        if name not in self.data:
            raise KeyError(name)
        return self.data[name]

    def has(self, name):
        return name in self.data

    def remove(self, name):
        self.timeline.append(("remove", name))
        return self.data.pop(name, None) is not None


class _GitHub:
    """Enough of github.com's reset flow. `password` is what GitHub holds."""

    def __init__(self, clock, timeline, inbox, request_page=REQUEST_PAGE, link_page=LINK_PAGE,
                 outcome="accept", send_email=True):
        self.clock, self.timeline, self.inbox = clock, timeline, inbox
        self.request_page, self.link_page = request_page, link_page
        self.outcome = outcome
        self.send_email = send_email
        self.password = OLD_PW
        self.posts = []

    def session(self):
        gh = self

        class _S:
            def request(self, method, url, form=None, referer=""):
                return gh.handle(method, url, form)

        return _S()

    def handle(self, method, url, form):
        self.timeline.append((method, url.split("/password_reset/")[0] + ("/<token>" if TOKEN in url else "")))
        if method == "GET" and url == "https://github.com/password_reset":
            return Page(200, url, self.request_page)
        if method == "POST" and url == "https://github.com/password_reset":
            assert form["authenticity_token"] == "tok-1"
            self.posts.append(("request", dict(form)))
            if self.send_email:
                self.inbox.deliver(form["email"])
            return Page(200, url, "<p>Check your email for a link to reset your password.</p>")
        if method == "GET" and url == LINK:
            return Page(200, url, self.link_page)
        if method == "POST" and url == LINK:
            self.posts.append(("submit", dict(form)))
            if self.outcome == "network":
                raise ResetError("github.com unreachable: timed out")
            if self.outcome == "refuse":
                return Page(200, url, '<div class="flash-error">Password is weak</div>' + LINK_PAGE)
            assert form["password"] == form["password_confirmation"]
            self.password = form["password"]
            if self.outcome == "two-factor":
                return Page(200, "https://github.com/sessions/two-factor/app", "<p>2FA</p>")
            return Page(200, "https://github.com/", "<p>Dashboard</p>")
        raise AssertionError(f"unexpected {method} {url}")


class _AgentMail:
    """Fake AgentMail transport -- the inbox for EMAIL."""

    def __init__(self, clock, old_messages=()):
        self.clock = clock
        self.messages = list(old_messages)
        self.keys = []

    def deliver(self, to):
        assert to == EMAIL
        self.messages.append({
            "message_id": f"<msg-{len(self.messages)}@github.com>",
            "from": "GitHub <noreply@github.com>",
            "subject": "[GitHub] Please reset your password",
            "timestamp": self.clock.iso(),
            "text": f"Use this link to reset your password: {LINK}\n",
            "html": f'<a href="{LINK}?a=1&amp;b=2">Reset</a>',
        })

    def __call__(self, method, url, headers, body, timeout):
        self.keys.append(headers["Authorization"])
        prefix = f"{AgentMailInbox.API}/inboxes/skewbald%40agentmail.to/messages"
        assert url.startswith(prefix), url
        rest = url[len(prefix):]
        if rest.startswith("?") or rest == "":
            return 200, {"count": len(self.messages),
                         "messages": [{k: v for k, v in m.items() if k not in ("text", "html")}
                                      for m in self.messages]}
        mid = rest.lstrip("/")
        from urllib.parse import unquote
        for m in self.messages:
            if m["message_id"] == unquote(mid):
                return 200, m
        return 404, {}


class _Resolver:
    def __init__(self, values):
        self.values = values

    def resolve_call(self, template, boundary):
        return boundary(self.values[template[len("{{secret:"):-2]])


class _Ref:
    def __init__(self, tags=None, provider="github", backend=""):
        self.name = REF
        self.sm_name = REF
        self.provider = provider
        self.backend = backend
        self.tags = {"reset_email": EMAIL, "reset_inbox_ref": "skewbald-agentmail-key"} \
            if tags is None else tags


class _Registry:
    def __init__(self):
        self.retags = []

    def retag(self, name, tags=None, backend=None):
        self.retags.append((name, dict(tags)))
        self.backend = backend


@pytest.fixture
def world(tmp_path):
    clock = _Clock()
    timeline = []
    mail = _AgentMail(clock)
    local = _Local(timeline, {REF: OLD_PW})
    audit = AuditChain(path=tmp_path / "audit.log", clock_path=tmp_path / ".clock")
    registry = _Registry()

    def build(gh=None, **kw):
        gh = gh or _GitHub(clock, timeline, mail)
        flow = InboxPasswordReset(
            _Resolver({"skewbald-agentmail-key": INBOX_KEY}), registry=registry, local_backend=local,
            audit=audit, session_factory=gh.session,
            inbox_factory=lambda kind, r, key_ref, email: AgentMailInbox(r, key_ref, email, transport=mail),
            clock=clock, sleep=clock.sleep, actor="skewbald", bindings_path=tmp_path / "rb.json", **kw)
        return flow, gh

    class W:
        pass

    w = W()
    w.__dict__.update(clock=clock, timeline=timeline, mail=mail, local=local, audit=audit,
                      registry=registry, build=build, tmp=tmp_path)
    return w


def test_reset_stores_before_submitting_and_github_takes_the_stored_value(world):
    flow, gh = world.build()
    report = flow.reset(_Ref())
    new = world.local.data[REF]
    assert new != OLD_PW and len(new) >= 30
    assert gh.password == new                      # GitHub took exactly what the vault holds
    assert world.local.data[REF + ".previous"] == OLD_PW
    assert REF + ".resetting" not in world.local.data
    t = world.timeline
    assert t.index(("store", REF)) < t.index(("POST", "https://github.com/<token>"))
    assert [s.name for s in report.steps][-5:] == ["link", "stash", "store", "submit", "record"]
    assert gh.posts[0][1]["email"] == EMAIL
    assert world.mail.keys and all(k == f"Bearer {INBOX_KEY}" for k in world.mail.keys)


def test_reset_records_audit_binding_and_tags(world):
    flow, _ = world.build()
    flow.reset(_Ref())
    entries = world.audit.entries()
    assert [e["result"] for e in entries] == ["ok:requested:github", "ok:reset:github"]
    assert all(e["action"] == "reset" and e["actor"] == "skewbald" and e["ts"] for e in entries)
    b = load_rotation_bindings(world.tmp / "rb.json")["github-password"]
    assert b.status == "real" and b.account == EMAIL
    name, tags = world.registry.retags[-1]
    assert name == REF and tags["password_reset_by"] == "skewbald"
    assert tags["password_reset_at"].startswith("2027-")
    assert world.registry.backend == "local"   # pinned to where the value was written


def test_no_value_or_link_in_report_audit_or_bindings(world):
    flow, _ = world.build()
    report = flow.reset(_Ref())
    new = world.local.data[REF]
    blob = json.dumps({"steps": [s.__dict__ for s in report.steps], "w": report.warnings,
                       "n": report.needs_person})
    blob += (world.tmp / "audit.log").read_text() + (world.tmp / "rb.json").read_text()
    blob += json.dumps(world.registry.retags)
    for secret in (new, OLD_PW, TOKEN, INBOX_KEY):
        assert secret not in blob


def test_dry_run_changes_nothing(world):
    flow, gh = world.build()
    report = flow.reset(_Ref(), dry_run=True)
    assert report.dry_run and report.warnings
    assert gh.posts == [] and world.local.data == {REF: OLD_PW}
    assert world.audit.entries() == []


def test_captcha_on_request_page_stops_before_anything(world):
    flow, gh = world.build(_GitHub(world.clock, world.timeline, world.mail, request_page=CAPTCHA_PAGE))
    with pytest.raises(HumanCheckRequired):
        flow.reset(_Ref())
    assert gh.posts == [] and world.local.data == {REF: OLD_PW}


def test_captcha_on_link_page_leaves_vault_untouched(world):
    page = LINK_PAGE.replace("</form>", '<div class="h-captcha" data-sitekey="x"></div></form>')
    flow, gh = world.build(_GitHub(world.clock, world.timeline, world.mail, link_page=page))
    with pytest.raises(HumanCheckRequired):
        flow.reset(_Ref())
    assert world.local.data == {REF: OLD_PW}
    assert [p[0] for p in gh.posts] == ["request"]


def test_refused_password_puts_previous_back(world):
    flow, gh = world.build(_GitHub(world.clock, world.timeline, world.mail, outcome="refuse"))
    with pytest.raises(ResetError, match="refused"):
        flow.reset(_Ref())
    assert world.local.data == {REF: OLD_PW}
    assert gh.password == OLD_PW


def test_refused_with_no_previous_removes_the_new_value(world):
    world.local.data.clear()
    flow, _ = world.build(_GitHub(world.clock, world.timeline, world.mail, outcome="refuse"))
    with pytest.raises(ResetError):
        flow.reset(_Ref())
    assert world.local.data == {}


def test_reset_works_when_no_password_was_ever_stored(world):
    world.local.data.clear()
    flow, gh = world.build()
    flow.reset(_Ref())
    assert world.local.data[REF] == gh.password
    assert REF + ".previous" not in world.local.data


def test_lost_answer_keeps_marker_and_blocks_next_run(world):
    flow, _ = world.build(_GitHub(world.clock, world.timeline, world.mail, outcome="network"))
    with pytest.raises(ResetError, match="--force"):
        flow.reset(_Ref())
    assert REF + ".resetting" in world.local.data
    assert world.local.data[REF + ".previous"] == OLD_PW
    flow2, _ = world.build()
    with pytest.raises(ResetError, match="did not finish"):
        flow2.reset(_Ref())
    flow2.reset(_Ref(), force=True)
    assert REF + ".resetting" not in world.local.data


def test_two_factor_after_submit_is_reported_not_failed(world):
    flow, gh = world.build(_GitHub(world.clock, world.timeline, world.mail, outcome="two-factor"))
    report = flow.reset(_Ref())
    assert "two-factor" in report.needs_person
    assert world.local.data[REF] == gh.password


def test_no_email_times_out(world):
    flow, _ = world.build(_GitHub(world.clock, world.timeline, world.mail, send_email=False),
                          email_wait=30, poll_interval=5)
    with pytest.raises(ResetError, match="no github reset email"):
        flow.reset(_Ref())
    assert world.local.data == {REF: OLD_PW}


def test_an_old_reset_email_is_not_used(world):
    world.mail.messages.append({
        "message_id": "<old@github.com>", "from": "noreply@github.com",
        "subject": "[GitHub] Please reset your password", "timestamp": world.clock.iso(-3600),
        "text": "https://github.com/password_reset/OLDtokenOLDtokenOLD"})
    flow, _ = world.build(_GitHub(world.clock, world.timeline, world.mail, send_email=False),
                          email_wait=10, poll_interval=5)
    with pytest.raises(ResetError, match="no github reset email"):
        flow.reset(_Ref())


@pytest.mark.parametrize("tags,match", [
    ({"reset_inbox_ref": "k"}, "reset_email"),
    ({"reset_email": EMAIL}, "reset_inbox_ref"),
    ({"reset_email": EMAIL, "reset_inbox_ref": "k", "reset_provider": "myspace"}, "no reset recipe"),
])
def test_missing_tags_are_explained(world, tags, match):
    flow, _ = world.build()
    with pytest.raises(ResetError, match=match):
        flow.reset(_Ref(tags=tags))


def test_non_local_backend_is_refused(world):
    flow, _ = world.build()
    with pytest.raises(ResetError, match="local vault only"):
        flow.reset(_Ref(backend="gcp"))


def test_parse_forms_flags_captcha_inside_form_only():
    forms, page_check = parse_forms(REQUEST_PAGE)
    assert [f.action for f in forms] == ["/search", "/password_reset"]
    assert not page_check and not any(f.human_check for f in forms)
    assert forms[1].fields() == {"authenticity_token": "tok-1", "email": ""}
    forms, _ = parse_forms(CAPTCHA_PAGE)
    assert forms[0].human_check
    forms, _ = parse_forms(LINK_PAGE)
    assert forms[0].password_fields() == ["password", "password_confirmation"]


def test_github_password_adapter_is_registered():
    adapter = rotation_adapter_for("github-password")
    assert adapter is not None and adapter.capability() == "auto"


def test_cli_reset_dry_run(tmp_path, monkeypatch, capsys):
    from portunus import cli

    monkeypatch.setenv("PORTUNUS_HOME", str(tmp_path))
    reg = Registry()
    reg.add(REF, REF, provider="github", tags={"reset_email": EMAIL, "reset_inbox_ref": "k"})
    calls = []

    class _Flow:
        def __init__(self, *a, **kw):
            pass

        def reset(self, ref, dry_run=False, force=False):
            calls.append((ref.name, dry_run))
            return mod.ResetReport(ref.name, "github", dry_run, (mod.Step("preflight", "ok"),),
                                   ("note",))

    monkeypatch.setattr(mod, "InboxPasswordReset", _Flow)
    assert cli.main(["reset", REF, "--dry-run"]) == 0
    out = capsys.readouterr().out
    assert "reset github" in out and "plan" in out and "warning" in out
    assert calls == [(REF, True)]


def test_cli_reset_human_check_exits_2(tmp_path, monkeypatch, capsys):
    from portunus import cli

    monkeypatch.setenv("PORTUNUS_HOME", str(tmp_path))
    reg = Registry()
    reg.add(REF, REF, provider="github", tags={"reset_email": EMAIL, "reset_inbox_ref": "k"})

    class _Flow:
        def __init__(self, *a, **kw):
            pass

        def reset(self, ref, dry_run=False, force=False):
            raise HumanCheckRequired("github shows a captcha")

    monkeypatch.setattr(mod, "InboxPasswordReset", _Flow)
    assert cli.main(["reset", REF, "--dry-run"]) == 2
    assert "stopped for a person" in capsys.readouterr().err
