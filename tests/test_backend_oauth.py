"""portunus-oauth-token-broker Story 03: OAuthBackend -- a SecretBackend
adapter that mints (and in-memory-caches) an access token on .access().
sm_name encodes "provider:account" -- an opaque, backend-specific
identifier, the same role GCP Secret Manager's own sm_name already plays.
Zero Resolver-side changes needed: this backend returns a plain string,
exactly like every other backend already does, so it flows through the
existing resolve/resolve_exec/resolve_to_tempfile boundary paths for free."""
import time

import pytest

from portunus import AuditChain, Broker, Registry, Resolver
from portunus.backend import BackendError, OAuthBackend
from portunus.localvault import LocalEncryptedBackend

CREDENTIAL = {
    "client_id": "client-123",
    "client_secret": "CLIENT-SECRET-do-not-leak",
    "refresh_token": "REFRESH-TOKEN-do-not-leak",
    "token_endpoint": "https://oauth2.example.com/token",
}


def _seed_credential(home, provider="google", account="user@example.com"):
    local = LocalEncryptedBackend()
    local.store_oauth_credential(provider, account, CREDENTIAL)
    return local


def _counting_transport(access_token="ACCESS.TOKEN", expires_in=3600):
    calls = []

    def transport(url, data, headers, timeout):
        calls.append(dict(data))
        return {"access_token": access_token, "expires_in": expires_in}

    return transport, calls


def test_oauth_backend_access_mints_a_real_token(home):
    _seed_credential(home)
    transport, calls = _counting_transport()
    backend = OAuthBackend(audit=AuditChain(), transport=transport)
    token = backend.access("google:user@example.com")
    assert token == "ACCESS.TOKEN"
    assert len(calls) == 1
    assert calls[0]["refresh_token"] == "REFRESH-TOKEN-do-not-leak"


def test_oauth_backend_caches_within_expiry_skew(home):
    _seed_credential(home)
    transport, calls = _counting_transport(expires_in=3600)
    backend = OAuthBackend(audit=AuditChain(), transport=transport)
    backend.access("google:user@example.com")
    backend.access("google:user@example.com")
    assert len(calls) == 1  # second call served from cache, no re-mint


def test_oauth_backend_remints_after_expiry_skew(home, monkeypatch):
    _seed_credential(home)
    transport, calls = _counting_transport(expires_in=60)
    backend = OAuthBackend(audit=AuditChain(), transport=transport, skew_seconds=30)
    backend.access("google:user@example.com")

    real_time = time.time
    monkeypatch.setattr(time, "time", lambda: real_time() + 3600)
    backend.access("google:user@example.com")
    assert len(calls) == 2


def test_oauth_backend_unknown_credential_raises_backend_error(home):
    transport, _calls = _counting_transport()
    backend = OAuthBackend(audit=AuditChain(), transport=transport)
    with pytest.raises(BackendError):
        backend.access("google:nobody@example.com")


def test_oauth_backend_mint_failure_raises_backend_error(home):
    _seed_credential(home)

    def failing_transport(url, data, headers, timeout):
        return {"error": "invalid_grant"}  # no access_token

    backend = OAuthBackend(audit=AuditChain(), transport=failing_transport)
    with pytest.raises(BackendError):
        backend.access("google:user@example.com")


def test_oauth_backend_malformed_sm_name_raises_backend_error(home):
    transport, _calls = _counting_transport()
    backend = OAuthBackend(audit=AuditChain(), transport=transport)
    with pytest.raises(BackendError):
        backend.access("no-colon-here")


def test_oauth_backend_persists_a_rotated_refresh_token(home):
    # Some providers (confirmed live for Codex CLI's refresh grant against
    # auth.openai.com/oauth/token, 2026-09-14) return a NEW refresh_token on
    # every refresh -- the one just spent is dead for future refreshes. The
    # backend must persist the rotated value immediately so the next
    # access() (once the in-memory cache expires) doesn't mint against an
    # already-consumed token.
    _seed_credential(home)

    def transport(url, data, headers, timeout):
        return {"access_token": "ACCESS.1", "expires_in": 60, "refresh_token": "ROTATED-REFRESH-TOKEN"}

    backend = OAuthBackend(audit=AuditChain(), transport=transport)
    backend.access("google:user@example.com")

    local = LocalEncryptedBackend()
    record = local.load_oauth_credential("google", "user@example.com")
    assert record["credential"]["refresh_token"] == "ROTATED-REFRESH-TOKEN"
    # Everything else about the stored bundle is untouched.
    assert record["credential"]["client_id"] == CREDENTIAL["client_id"]
    assert record["credential"]["client_secret"] == CREDENTIAL["client_secret"]


def test_oauth_backend_no_rotation_in_response_leaves_stored_credential_untouched(home):
    _seed_credential(home)

    def transport(url, data, headers, timeout):
        return {"access_token": "ACCESS.1", "expires_in": 60}  # no refresh_token key at all

    backend = OAuthBackend(audit=AuditChain(), transport=transport)
    backend.access("google:user@example.com")

    local = LocalEncryptedBackend()
    record = local.load_oauth_credential("google", "user@example.com")
    assert record["credential"]["refresh_token"] == CREDENTIAL["refresh_token"]


def test_oauth_backend_mints_for_a_public_client_credential_with_no_secret(home):
    # A genuinely secret-less public OAuth client -- e.g. Codex CLI's real
    # refresh grant against auth.openai.com/oauth/token, confirmed live
    # 2026-09-14 against codex-rs's own source (codex-rs/login/src/auth/
    # manager.rs: CLIENT_ID = "app_EMoamEEZ73f0CkXaXp7hrann", no
    # client_secret field in the request at all). A stored bundle that
    # simply never had a client_secret key must still mint cleanly instead
    # of KeyError-ing before ever reaching the token endpoint.
    local = LocalEncryptedBackend()
    local.store_oauth_credential(
        "codex",
        "dostal",
        {
            "client_id": "app_EMoamEEZ73f0CkXaXp7hrann",
            "refresh_token": "REFRESH-TOKEN-do-not-leak",
            "token_endpoint": "https://auth.openai.com/oauth/token",
            "request_format": "json",
        },
    )
    transport, calls = _counting_transport()
    backend = OAuthBackend(audit=AuditChain(), transport=transport)
    token = backend.access("codex:dostal")
    assert token == "ACCESS.TOKEN"
    assert "client_secret" not in calls[0]
    assert calls[0]["client_id"] == "app_EMoamEEZ73f0CkXaXp7hrann"


def test_oauth_backend_flows_through_the_real_resolve_boundary(home):
    """Zero Resolver-side changes needed -- proving this by actually
    resolving a reference through the normal Registry/Broker/Resolver
    stack, not just calling OAuthBackend directly."""
    _seed_credential(home, provider="google", account="user@example.com")
    transport, _calls = _counting_transport(access_token="REAL.ACCESS.TOKEN")

    registry = Registry()
    registry.add("my-gmail-token", "google:user@example.com", backend="oauth")
    audit = AuditChain()
    broker = Broker(registry, audit)
    oauth_backend = OAuthBackend(audit=audit, transport=transport)
    resolver = Resolver(registry, oauth_backend, broker)

    seen = {}
    resolver.resolve_call(
        "Authorization: Bearer {{secret:my-gmail-token}}",
        lambda resolved: seen.setdefault("text", resolved),
    )
    assert "REAL.ACCESS.TOKEN" in seen["text"]


# --- router wiring ------------------------------------------------------

def test_make_backend_router_returns_oauth_backend_for_backend_oauth(home):
    from portunus.audit import AuditChain as AC
    from portunus.cli import _make_backend_router
    from portunus.localvault import LocalEncryptedBackend as LEB
    from portunus.registry import Reference

    router = _make_backend_router({}, AC(), LEB())
    ref = Reference(name="x", sm_name="google:user@example.com", backend="oauth")
    assert isinstance(router(ref), OAuthBackend)


def test_drop_backend_choices_include_oauth():
    from portunus.cli import build_parser

    parser = build_parser()
    # parse_args exits (SystemExit) on an invalid --backend choice -- if
    # "oauth" weren't a valid choice this would raise before ever
    # reaching cmd_drop.
    args = parser.parse_args(["drop", "name", "sm", "--backend", "oauth", "--stdin"])
    assert args.backend == "oauth"


def test_bindings_set_backend_choices_include_oauth():
    from portunus.cli import build_parser

    parser = build_parser()
    args = parser.parse_args(["bindings", "set", "my-project", "--backend", "oauth"])
    assert args.backend == "oauth"
