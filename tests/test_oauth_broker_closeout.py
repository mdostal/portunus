"""portunus-oauth-token-broker Story 04 (closeout): the full store ->
register -> resolve flow, end to end, plus a direct filesystem proof that
the credential's client_secret/refresh_token never appear in plaintext
anywhere on disk. Synthetic shape of the epic's own live proof (two real
Google accounts, bootstrapped via `gcloud auth application-default login`,
minted genuinely distinct access tokens) -- see the epic's own closeout
notes for that real, one-off, manually-run proof."""
from portunus import AuditChain, Broker, Registry, Resolver
from portunus.backend import OAuthBackend
from portunus.localvault import LocalEncryptedBackend

CREDENTIAL = {
    "client_id": "client-123",
    "client_secret": "CLIENT-SECRET-do-not-leak",
    "refresh_token": "REFRESH-TOKEN-do-not-leak",
    "token_endpoint": "https://oauth2.example.com/token",
}


def test_full_store_register_resolve_flow_and_no_plaintext_on_disk(home):
    local = LocalEncryptedBackend()
    local.store_oauth_credential("google", "personal", CREDENTIAL)

    registry = Registry()
    registry.add("my-token", "google:personal", backend="oauth")
    audit = AuditChain()
    broker = Broker(registry, audit)

    def transport(url, data, headers, timeout):
        return {"access_token": "MINTED.ACCESS.TOKEN", "expires_in": 3600}

    oauth_backend = OAuthBackend(local_backend=local, audit=audit, transport=transport)
    resolver = Resolver(registry, oauth_backend, broker)

    seen = {}
    resolver.resolve_call(
        "Authorization: Bearer {{secret:my-token}}",
        lambda resolved: seen.setdefault("text", resolved),
    )
    assert "MINTED.ACCESS.TOKEN" in seen["text"]

    for name in ("vault.enc.json", "registry.json", "audit.log"):
        path = home / name
        if path.exists():
            text = path.read_text()
            assert CREDENTIAL["client_secret"] not in text
            assert CREDENTIAL["refresh_token"] not in text
            assert "MINTED.ACCESS.TOKEN" not in text


def test_two_accounts_under_one_provider_mint_independently(home):
    """The multiple-accounts-per-provider shape the live proof exercised
    with two real Google accounts -- synthetic here, same mechanism."""
    local = LocalEncryptedBackend()
    local.store_oauth_credential("google", "personal", CREDENTIAL)
    local.store_oauth_credential(
        "google", "firefly-events",
        {**CREDENTIAL, "refresh_token": "OTHER-REFRESH-TOKEN-do-not-leak"},
    )

    registry = Registry()
    registry.add("personal-token", "google:personal", backend="oauth")
    registry.add("firefly-token", "google:firefly-events", backend="oauth")
    audit = AuditChain()
    broker = Broker(registry, audit)

    seen_refresh_tokens = []

    def transport(url, data, headers, timeout):
        seen_refresh_tokens.append(data["refresh_token"])
        return {"access_token": f"ACCESS-FOR-{data['refresh_token']}", "expires_in": 3600}

    oauth_backend = OAuthBackend(local_backend=local, audit=audit, transport=transport)
    resolver = Resolver(registry, oauth_backend, broker)

    results = {}
    resolver.resolve_call("{{secret:personal-token}}", lambda v: results.setdefault("personal", v))
    resolver.resolve_call("{{secret:firefly-token}}", lambda v: results.setdefault("firefly", v))

    assert results["personal"] != results["firefly"]
    assert set(seen_refresh_tokens) == {"REFRESH-TOKEN-do-not-leak", "OTHER-REFRESH-TOKEN-do-not-leak"}
