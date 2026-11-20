"""End-to-end: `browser_fill.fill` against a real headless Chromium (PANT-1400).

Two local HTTP servers on different sites (127.0.0.1 and localhost) give a
checkout page with a cross-site iframe, which Chromium runs out of process
(--site-per-process) -- the same shape as Stripe's card iframe. Skipped when
no Chromium binary is found: set PORTUNUS_TEST_CHROME, or have a Playwright
browser cache in ~/.cache/ms-playwright.
"""
from __future__ import annotations

import glob
import http.server
import json
import os
import shutil
import socket
import subprocess
import tempfile
import threading
import time
import urllib.request
from pathlib import Path

import pytest

from portunus.audit import AuditChain
from portunus.backend import MockBackend
from portunus.broker import ApprovalRequired, Broker
from portunus.browser_fill import BrowserFillError, FieldSpec, fill, parse_field_spec
from portunus.registry import Registry
from portunus.resolver import Resolver

CARD = {"number": "4242424242424242", "exp_month": 7, "exp_year": 2031, "cvc": "987",
        "name": "Ada Lovelace", "postal": "94110"}


def _find_chrome() -> str:
    env = os.environ.get("PORTUNUS_TEST_CHROME", "")
    if env:
        return env
    home = Path.home() / ".cache" / "ms-playwright"
    for pattern in ("chromium_headless_shell-*/*/chrome-headless-shell", "chromium-*/*/chrome"):
        hits = sorted(glob.glob(str(home / pattern)))
        if hits:
            return hits[-1]
    for name in ("chromium", "chromium-browser", "google-chrome"):
        path = shutil.which(name)
        if path:
            return path
    return ""


CHROME = _find_chrome()
pytestmark = pytest.mark.skipif(not CHROME, reason="no Chromium binary available")


def _free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def _serve(directory: str, host: str) -> int:
    port = _free_port()

    class Handler(http.server.SimpleHTTPRequestHandler):
        def __init__(self, *a, **kw):
            super().__init__(*a, directory=directory, **kw)

        def log_message(self, *a):
            pass

    srv = http.server.ThreadingHTTPServer((host, port), Handler)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    return port


@pytest.fixture(scope="module")
def site(tmp_path_factory):
    root = tmp_path_factory.mktemp("site")
    frame_port = _serve(str(root), "127.0.0.1")  # served as http://localhost:<port>
    top_port = _serve(str(root), "127.0.0.1")
    (root / "frame.html").write_text(
        "<html><body><input name='cardnumber'><input name='exp'><input name='cvc'>"
        "<script>document.querySelector('[name=cvc]').addEventListener('input',"
        "e=>{document.body.dataset.seen=e.inputType||'synthetic'})</script>"
        "</body></html>")
    (root / "checkout.html").write_text(
        "<html><body><input id='email'><input id='pw' type='password'><input class='dup'>"
        f"<input class='dup'><iframe id='f' src='http://localhost:{frame_port}/frame.html'></iframe>"
        "</body></html>")
    return {"top": f"http://127.0.0.1:{top_port}", "frame": f"http://localhost:{frame_port}"}


@pytest.fixture(scope="module")
def chrome(site):
    port = _free_port()
    profile = tempfile.mkdtemp(prefix="portunus-chrome-")
    proc = subprocess.Popen(
        [CHROME, "--headless", f"--remote-debugging-port={port}", "--remote-debugging-address=127.0.0.1",
         f"--user-data-dir={profile}", "--no-first-run", "--no-sandbox", "--site-per-process",
         "--disable-gpu", site["top"] + "/checkout.html"],
        stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    endpoint = f"http://127.0.0.1:{port}"
    deadline = time.time() + 20
    while time.time() < deadline:
        try:
            urllib.request.urlopen(endpoint + "/json/version", timeout=1).read()
            break
        except Exception:
            time.sleep(0.2)
    else:
        proc.kill()
        pytest.skip("Chromium did not expose a DevTools endpoint")
    time.sleep(1.5)  # let the iframe load
    yield endpoint
    proc.kill()
    proc.wait()
    shutil.rmtree(profile, ignore_errors=True)


def _resolver(home, allowed, approval=""):
    reg = Registry()
    reg.add("card-1", "card-1", approval=approval)
    reg.add("login-pw", "login-pw")
    if allowed:
        reg.set_allowed_origins("card-1", allowed)
        reg.set_allowed_origins("login-pw", allowed)
    audit = AuditChain()
    broker = Broker(reg, audit)
    backend = MockBackend({"card-1": json.dumps(CARD), "login-pw": "hunter2-very-secret"})
    return Resolver(reg, backend, broker), audit


def _without_noise(text, site):
    """Drop the test servers' origins (random ports) so a short secret can't
    match a port number by chance."""
    for origin in (site["top"], site["frame"]):
        text = text.replace(origin, "<origin>")
    return text


def _values(endpoint, expression_by_frame):
    """Read field values back through CDP -- test-only, to prove they landed."""
    from portunus.browser_fill import _Page, _pick_tab
    from portunus.cdp import CDPConnection, browser_ws_url
    conn = CDPConnection(browser_ws_url(endpoint))
    try:
        page = _Page(conn, _pick_tab(conn, "checkout")["targetId"])
        out = {}
        for origin, expr in expression_by_frame:
            fr = next(f for f in page.frames if f.origin == origin)
            out[(origin, expr)] = page._eval(fr.session, fr.context_id, expr)
        return out
    finally:
        conn.close()


def test_card_fields_land_in_a_cross_site_iframe_and_the_result_has_no_value(home, site, chrome):
    resolver, audit = _resolver(home, [site["top"], site["frame"]])
    result = fill(resolver, "card-1", [
        parse_field_spec("number=input[name=cardnumber]"),
        parse_field_spec("expiry=input[name=exp]"),
        parse_field_spec("cvc=input[name=cvc]"),
    ], tab="checkout", cdp=chrome)
    blob = _without_noise(json.dumps(result), site)
    for secret in ("4242424242424242", "987", "07/31"):
        assert secret not in blob
    assert [f["typed"] for f in result["fields"]] == [16, 5, 3]
    assert {f["origin"] for f in result["fields"]} == {site["frame"]}
    assert result["tab_origin"] == site["top"]

    got = _values(chrome, [(site["frame"], "document.querySelector('[name=cardnumber]').value"),
                           (site["frame"], "document.querySelector('[name=exp]').value"),
                           (site["frame"], "document.querySelector('[name=cvc]').value"),
                           (site["frame"], "document.body.dataset.seen")])
    vals = list(got.values())
    assert vals[:3] == ["4242424242424242", "07/31", "987"]
    # the page's own listener saw a real typed input, not a synthetic event
    assert vals[3] == "insertText"
    assert {f["method"] for f in result["fields"]} == {"keyboard"}

    log = audit.path.read_text()
    assert log.count("browser_fill") == 3 and site["frame"] in log and site["top"] in log
    # Short values like a 3-digit CVC can turn up by chance in the chain's
    # hex hashes, timestamps or the servers' random ports, so check only the
    # fields that could carry a value, with the known origins taken out.
    entries = [json.loads(line) for line in log.splitlines() if line.strip()]
    for entry in entries:
        for noise in ("h", "prev", "ts", "seq"):
            entry.pop(noise, None)
    carried = _without_noise(json.dumps(entries), site)
    for secret in ("4242424242424242", "987", "07/31"):
        assert secret not in carried


def test_whole_value_into_the_top_frame(home, site, chrome):
    resolver, _ = _resolver(home, [site["top"]])
    result = fill(resolver, "login-pw", [FieldSpec("", "#pw")], tab="checkout", cdp=chrome)
    assert result["fields"][0]["typed"] == len("hunter2-very-secret")
    got = _values(chrome, [(site["top"], "document.getElementById('pw').value")])
    assert list(got.values()) == ["hunter2-very-secret"]


def test_frame_origin_not_on_the_list_is_refused_before_resolving(home, site, chrome):
    resolver, _ = _resolver(home, [site["top"]])  # iframe's origin missing
    calls = []
    real = resolver._fetch
    resolver._fetch = lambda n: calls.append(n) or real(n)
    with pytest.raises(BrowserFillError, match="may not be typed into " + site["frame"]):
        fill(resolver, "card-1", [FieldSpec("cvc", "input[name=cvc]")], tab="checkout", cdp=chrome)
    assert calls == []


def test_top_origin_not_on_the_list_is_refused(home, site, chrome):
    resolver, _ = _resolver(home, [site["frame"]])  # only the iframe
    with pytest.raises(BrowserFillError, match="may not be typed into " + site["top"]):
        fill(resolver, "card-1", [FieldSpec("cvc", "input[name=cvc]")], tab="checkout", cdp=chrome)


def test_ambiguous_selector_is_refused(home, site, chrome):
    resolver, _ = _resolver(home, [site["top"], site["frame"]])
    with pytest.raises(BrowserFillError, match="matches 2 elements"):
        fill(resolver, "login-pw", [FieldSpec("", "input.dup")], tab="checkout", cdp=chrome)


def test_gated_secret_is_not_typed_without_approval(home, site, chrome):
    resolver, _ = _resolver(home, [site["top"], site["frame"]], approval="required")
    with pytest.raises(ApprovalRequired):
        fill(resolver, "card-1", [FieldSpec("cvc", "input[name=cvc]")], tab="checkout", cdp=chrome)
    resolver.broker.approve("card-1")
    result = fill(resolver, "card-1", [FieldSpec("cvc", "input[name=cvc]")], tab="checkout", cdp=chrome)
    assert result["fields"][0]["typed"] == 3
