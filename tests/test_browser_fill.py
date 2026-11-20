"""`portunus browser fill` without a browser (PANT-1400): origin and field
parsing, structured keys, the refusals that happen before anything connects,
and the CLI/MCP surfaces. The real-Chromium path is in
test_browser_fill_chrome.py."""
from __future__ import annotations

import json

import pytest

from portunus import cli, mcp_server
from portunus.audit import AuditChain
from portunus.backend import MockBackend
from portunus.broker import Broker
from portunus.browser_fill import (
    BrowserFillError, FieldSpec, field_value, fill, normalize_origin, origin_allowed,
    parse_field_spec, split_ref,
)
from portunus.cdp import CDPError, browser_ws_url, check_loopback
from portunus.registry import Registry
from portunus.resolver import Resolver

CARD = json.dumps({"number": "4242424242424242", "exp_month": 7, "exp_year": 2031,
                   "cvc": "987", "name": "Ada Lovelace", "postal": "94110"})


# --- origins ---------------------------------------------------------------
@pytest.mark.parametrize("raw,want", [
    ("https://Checkout.Stripe.com/pay?x=1", "https://checkout.stripe.com"),
    ("https://example.com:443", "https://example.com"),
    ("http://localhost:8080/", "http://localhost:8080"),
    ("http://[::1]:3000", "http://[::1]:3000"),
])
def test_normalize_origin(raw, want):
    assert normalize_origin(raw) == want


@pytest.mark.parametrize("raw", ["checkout.stripe.com", "https://*.stripe.com", "file:///tmp/x",
                                 "javascript:alert(1)", "", "null"])
def test_normalize_origin_rejects(raw):
    with pytest.raises(ValueError):
        normalize_origin(raw)


def test_origin_allowed_is_exact():
    allowed = ["https://checkout.stripe.com"]
    assert origin_allowed("https://checkout.stripe.com", allowed)
    assert not origin_allowed("https://evil.checkout.stripe.com", allowed)
    assert not origin_allowed("http://checkout.stripe.com", allowed)
    assert not origin_allowed("https://checkout.stripe.com.evil.com", allowed)
    assert not origin_allowed("null", allowed)


# --- structured values ---------------------------------------------------------
@pytest.mark.parametrize("key,want", [
    ("", CARD), ("number", "4242424242424242"), ("cvc", "987"), ("cvv", "987"),
    ("postal_code", "94110"), ("zip", "94110"), ("expiry", "07/31"), ("exp_mm", "07"),
    ("exp_yy", "31"), ("exp_yyyy", "2031"), ("exp_month", "7"), ("name", "Ada Lovelace"),
])
def test_field_value(key, want):
    assert field_value(CARD, key) == want


def test_field_value_errors_never_contain_the_value():
    with pytest.raises(BrowserFillError) as exc:
        field_value(CARD, "iban", "card-1")
    assert "4242" not in str(exc.value) and "987" not in str(exc.value)
    assert "card-1#iban" in str(exc.value) and "number" in str(exc.value)
    with pytest.raises(BrowserFillError) as exc:
        field_value("sk-live-plain-token", "number", "tok")
    assert "sk-live" not in str(exc.value)


def test_split_ref_and_field_specs():
    assert split_ref("free-reign-card-1#number") == ("free-reign-card-1", "number")
    assert split_ref("plain") == ("plain", "")
    spec = parse_field_spec("cvc=input[name=cvc]@https://js.stripe.com")
    assert spec == FieldSpec("cvc", "input[name=cvc]", "https://js.stripe.com")
    assert parse_field_spec("=#pw") == FieldSpec("", "#pw", "")
    # an @ inside a selector that isn't followed by an origin stays in the selector
    assert parse_field_spec("=a[href^=mailto:x@y]").selector == "a[href^=mailto:x@y]"
    with pytest.raises(ValueError):
        parse_field_spec("input[name=cvc]")


# --- endpoint ------------------------------------------------------------------
@pytest.mark.parametrize("url", ["http://10.0.0.5:9222", "ws://evil.example.com/devtools/browser/x",
                                 "https://127.0.0.1:9222", "http://169.254.1.1:9222"])
def test_cdp_endpoint_must_be_loopback(url):
    with pytest.raises(CDPError):
        check_loopback(url)


def test_loopback_endpoints_pass():
    for url in ("http://127.0.0.1:9222", "http://localhost:9222", "ws://[::1]:9222/devtools/browser/a"):
        check_loopback(url)


def test_discovery_refuses_a_non_loopback_websocket(monkeypatch):
    class Resp:
        def __enter__(self):
            return self

        def __exit__(self, *a):
            pass

        def read(self):
            return json.dumps({"webSocketDebuggerUrl": "ws://203.0.113.9:9222/devtools/browser/x"}).encode()

    monkeypatch.setattr("urllib.request.urlopen", lambda *a, **kw: Resp())
    with pytest.raises(CDPError, match="not on loopback"):
        browser_ws_url("http://127.0.0.1:9222")


# --- refusals before connecting ---------------------------------------------------
def _resolver(home):
    reg = Registry()
    reg.add("card-1", "card-1")
    audit = AuditChain()
    return Resolver(reg, MockBackend({"card-1": CARD}), Broker(reg, audit)), audit


def test_no_allowed_origins_refuses_without_connecting_or_resolving(home):
    resolver, audit = _resolver(home)

    def boom(_endpoint):
        raise AssertionError("must not connect")

    with pytest.raises(BrowserFillError, match="no allowed origins"):
        fill(resolver, "card-1", [FieldSpec("cvc", "#cvc")], connect=boom)
    log = audit.path.read_text()
    assert "denied-no-allowed-origins" in log and '"resolve"' not in log


def test_registry_keeps_allowed_origins_and_omits_them_when_empty(home):
    reg = Registry()
    reg.add("a", "a")
    reg.add("b", "b")
    reg.set_allowed_origins("a", ["https://checkout.stripe.com", "https://checkout.stripe.com"])
    raw = json.loads(reg.path.read_text())
    assert raw["a"]["allowed_origins"] == ["https://checkout.stripe.com"]
    assert "allowed_origins" not in raw["b"]
    assert Registry().get("a").allowed_origins == ["https://checkout.stripe.com"]


# --- CLI ---------------------------------------------------------------------------
def test_cli_allow_disallow_origins(home, capsys):
    Registry().add("card-1", "card-1")
    assert cli.main(["browser", "allow", "card-1", "https://Checkout.Stripe.com/x", "https://js.stripe.com"]) == 0
    assert Registry().get("card-1").allowed_origins == ["https://checkout.stripe.com", "https://js.stripe.com"]
    assert cli.main(["browser", "disallow", "card-1", "https://js.stripe.com"]) == 0
    capsys.readouterr()
    assert cli.main(["browser", "origins", "card-1", "--json"]) == 0
    assert json.loads(capsys.readouterr().out)["allowed_origins"] == ["https://checkout.stripe.com"]
    assert cli.main(["browser", "allow", "card-1", "https://*.evil.com"]) != 0
    assert cli.main(["browser", "disallow", "card-1", "--all"]) == 0
    assert Registry().get("card-1").allowed_origins == []
    assert "browser_allow" in AuditChain().path.read_text()


def test_cli_fill_refusal_prints_no_value(home, capsys, monkeypatch):
    monkeypatch.setenv("PORTUNUS_BACKEND", "mock")
    monkeypatch.setenv("PORTUNUS_MOCK_CARD_1", CARD)
    Registry().add("card-1", "card-1")
    rc = cli.main(["browser", "fill", "card-1#number", "--field", "=input[name=cardnumber]"])
    out = capsys.readouterr()
    assert rc != 0
    assert "no allowed origins" in out.err
    assert "4242" not in out.out + out.err


def test_cli_fill_rejects_a_remote_cdp_endpoint(home, capsys, monkeypatch):
    monkeypatch.setenv("PORTUNUS_BACKEND", "mock")
    monkeypatch.setenv("PORTUNUS_MOCK_CARD_1", CARD)
    Registry().add("card-1", "card-1")
    Registry().set_allowed_origins("card-1", ["https://checkout.stripe.com"])
    rc = cli.main(["browser", "fill", "card-1", "--field", "cvc=#cvc", "--cdp", "http://203.0.113.9:9222"])
    assert rc != 0
    assert "not on loopback" in capsys.readouterr().err


# --- MCP ---------------------------------------------------------------------------
def test_mcp_tool_refuses_without_allow_list(home, monkeypatch):
    monkeypatch.setenv("PORTUNUS_BACKEND", "mock")
    monkeypatch.setenv("PORTUNUS_MOCK_CARD_1", CARD)
    Registry().add("card-1", "card-1")
    out = mcp_server.portunus_browser_fill("card-1#number", [{"selector": "#n"}])
    assert "no allowed origins" in out["error"]
    assert "4242" not in json.dumps(out)
    assert "selector" in mcp_server.portunus_browser_fill("card-1", [{"key": "cvc"}])["error"]
    assert "unknown reference" in mcp_server.portunus_browser_fill("nope", [{"selector": "#n"}])["error"]


def test_mcp_has_no_tool_to_change_the_allow_list():
    names = [n for n in dir(mcp_server) if n.startswith("portunus_")]
    assert "portunus_browser_fill" in names
    assert not [n for n in names if "allow" in n or "origin" in n]
