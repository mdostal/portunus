"""Playwright-style storageState injection through Portunus."""
from datetime import datetime, timedelta, timezone

import pytest

from portunus import AuditChain, Broker, LocalEncryptedBackend, Registry
from portunus.localvault import LocalEncryptedBackend as LocalVault
from portunus.playwright import (
    PLAYWRIGHT_SESSION_KIND,
    PlaywrightSessionAdapter,
    SessionExpired,
    SessionRevoked,
)


SITE = "gig-radar"
ACCOUNT = "dostal"
COOKIE_VALUE = "PW-SESSION-do-not-leak-0xA11CE"


def _storage_state(cookie_value=COOKIE_VALUE):
    return {
        "cookies": [
            {
                "name": "session",
                "value": cookie_value,
                "domain": "gig-radar.test",
                "path": "/",
                "httpOnly": True,
            }
        ],
        "origins": [
            {
                "origin": "https://gig-radar.test",
                "localStorage": [{"name": "auth", "value": "logged-in"}],
            }
        ],
    }


class FakeBrowser:
    def __init__(self):
        self.last_context = None
        self.last_storage_state = None

    def new_context(self, *, storage_state, **kwargs):
        self.last_storage_state = storage_state
        self.last_context = FakeContext(storage_state, kwargs)
        return self.last_context


class FakeContext:
    def __init__(self, storage_state, kwargs):
        self.storage_state = storage_state
        self.kwargs = kwargs
        self.closed = False

    def is_authenticated(self):
        return any(
            cookie.get("name") == "session" and cookie.get("value") == COOKIE_VALUE
            for cookie in self.storage_state.get("cookies", [])
        )

    def close(self):
        self.closed = True


def _stack(now=None):
    backend = LocalEncryptedBackend()
    registry = Registry()
    audit = AuditChain()
    broker = Broker(registry, audit)
    adapter = PlaywrightSessionAdapter(
        backend,
        registry=registry,
        broker=broker,
        now=now,
    )
    return backend, registry, broker, adapter


def test_saved_valid_playwright_session_injects_authenticated_context(home):
    _, registry, _, adapter = _stack()
    browser = FakeBrowser()

    inspection = adapter.save_storage_state(
        SITE,
        ACCOUNT,
        _storage_state(),
        ttl_seconds=3600,
    )
    context = adapter.new_context(browser, SITE, ACCOUNT, viewport={"width": 1280, "height": 720})

    ref = registry.require(LocalVault.session_key(SITE, ACCOUNT))
    assert ref.kind == PLAYWRIGHT_SESSION_KIND
    assert inspection["namespace"] == {"site": SITE, "account": ACCOUNT}
    assert context.is_authenticated()
    assert browser.last_context.kwargs == {"viewport": {"width": 1280, "height": 720}}


def test_refreshing_playwright_session_preserves_existing_approval_gate(home):
    _, registry, broker, adapter = _stack()
    name = LocalVault.session_key(SITE, ACCOUNT)
    adapter.save_storage_state(SITE, ACCOUNT, _storage_state("first"), ttl_seconds=3600)
    broker.gate(name, on=True)

    adapter.save_storage_state(SITE, ACCOUNT, _storage_state("second"), ttl_seconds=3600)

    assert registry.require(name).approval == "required"


def test_injected_storage_state_is_dropped_when_context_closes(home):
    _, _, _, adapter = _stack()
    browser = FakeBrowser()
    adapter.save_storage_state(SITE, ACCOUNT, _storage_state(), ttl_seconds=3600)

    context = adapter.new_context(browser, SITE, ACCOUNT)
    injected_state = browser.last_storage_state
    assert injected_state["cookies"]

    context.close()

    assert browser.last_context.closed is True
    assert injected_state == {}


def test_expired_playwright_session_is_refused(home):
    _, _, _, adapter = _stack()
    inspection = adapter.save_storage_state(SITE, ACCOUNT, _storage_state(), ttl_seconds=3600)
    expires_at = datetime.fromisoformat(
        inspection["ttl"]["expires_at"].replace("Z", "+00:00")
    ).astimezone(timezone.utc)

    with pytest.raises(SessionExpired, match="re-authentication required"):
        PlaywrightSessionAdapter(
            adapter.backend,
            registry=adapter.registry,
            broker=adapter.broker,
            now=lambda: expires_at + timedelta(seconds=1),
        ).new_context(FakeBrowser(), SITE, ACCOUNT)


def test_revoked_playwright_session_fails_auth_and_is_removed(home):
    backend, registry, _, adapter = _stack()
    adapter.save_storage_state(SITE, ACCOUNT, _storage_state(), ttl_seconds=3600)
    registry.set_state(LocalVault.session_key(SITE, ACCOUNT), "revoked")

    with pytest.raises(SessionRevoked, match="requires re-authentication"):
        adapter.new_context(FakeBrowser(), SITE, ACCOUNT)

    assert backend.remove_session(SITE, ACCOUNT) is False
