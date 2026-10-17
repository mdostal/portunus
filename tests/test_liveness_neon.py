"""Neon Postgres liveness (PANT-1120): a real connect + `SELECT 1` against
the stored connection string. Unit tests inject the connector; the
end-to-end test drives the real pgwire client against test_pgwire's fake
server. No real database is contacted."""
import json

import pytest

from portunus import liveness, pgwire
from portunus.liveness import NeonLivenessAdapter

PASSWORD = "FAKE-neon-pw-0xBEEF"
URL = (f"postgresql://neondb_owner:{PASSWORD}@ep-cool-name-a1b2c3.us-east-2.aws.neon.tech"
       "/neondb?sslmode=require&channel_binding=require")


class FakeConnector:
    def __init__(self, exc=None):
        self.exc = exc
        self.calls = []

    def __call__(self, info, timeout):
        self.calls.append(info)
        if self.exc is not None:
            raise self.exc


def _add(stack, value=URL, name="shop-mdostal-database-url-dev", provider="neon", tags=None):
    stack["registry"].add(name, f"sm-{name}", provider=provider, tags=tags)
    stack["backend"].set(f"sm-{name}", value)
    return stack["registry"].get(name)


def _check(stack, ref, connector):
    return liveness.check_reference(ref, stack["resolver"], adapter=NeonLivenessAdapter(connector))


def _assert_no_leak(result):
    blob = json.dumps(result.to_dict())
    assert PASSWORD not in blob and "neondb_owner" not in blob and "channel_binding" not in blob


def test_registered_under_neon():
    assert "neon" in liveness.liveness_providers()
    assert isinstance(liveness.liveness_adapter_for("neon"), NeonLivenessAdapter)


def test_live(stack):
    conn = FakeConnector()
    result = _check(stack, _add(stack), conn)
    assert result.status == "live" and result.http_status is None
    assert result.detail == "SELECT 1 @ ep-cool-name-a1b2c3.us-east-2.aws.neon.tech/neondb -> ok"
    info = conn.calls[0]
    assert (info.user, info.password, info.database, info.sslmode) == (
        "neondb_owner", PASSWORD, "neondb", "require")
    _assert_no_leak(result)


def test_rotated_password_is_dead(stack):
    """The 2026-10-16 incident: Neon rotated the branch password server-side."""
    result = _check(stack, _add(stack), FakeConnector(pgwire.PgError("rejected", "28P01")))
    assert result.status == "dead"
    assert result.detail.endswith("-> SQLSTATE 28P01")
    _assert_no_leak(result)


def test_invalid_authorization_is_dead(stack):
    assert _check(stack, _add(stack), FakeConnector(pgwire.PgError("x", "28000"))).status == "dead"


@pytest.mark.parametrize("exc", [
    pgwire.PgError("connect failed: TimeoutError"),
    pgwire.PgError("TLS handshake failed: SSLError"),
    pgwire.PgError("rejected", "3D000"),   # database does not exist
    pgwire.PgError("rejected", "XX000"),   # e.g. Neon endpoint suspended/not found
])
def test_non_credential_failures_are_unknown(stack, exc):
    result = _check(stack, _add(stack), FakeConnector(exc))
    assert result.status == "unknown"
    _assert_no_leak(result)


def test_value_that_is_not_a_connection_string_is_unknown_and_not_echoed(stack):
    conn = FakeConnector()
    result = _check(stack, _add(stack, value="sk-not-a-url-" + PASSWORD), conn)
    assert result.status == "unknown" and conn.calls == []
    assert "not usable" in result.detail
    _assert_no_leak(result)


def test_gated_reference_is_never_connected(stack):
    ref = _add(stack)
    stack["broker"].gate(ref.name)
    conn = FakeConnector()
    result = _check(stack, stack["registry"].get(ref.name), conn)
    assert result.status == "unknown" and conn.calls == []


def test_liveness_provider_tag_selects_neon(stack):
    ref = _add(stack, provider="", tags={"liveness_provider": "neon"})
    assert liveness.provider_for(ref) == "neon"
    assert [r.name for r in liveness.checkable_references(stack["registry"])] == [ref.name]


def test_end_to_end_against_fake_server(stack):
    from test_pgwire import FakePg

    pg = FakePg(auth="scram", password=PASSWORD)
    try:
        live = _check(stack, _add(stack, value=pg.url(password=PASSWORD), name="good"), None)
        dead = _check(stack, _add(stack, value=pg.url(password="stale"), name="stale"), None)
    finally:
        pg.close()
    assert (live.status, dead.status) == ("live", "dead")
    assert dead.detail.endswith("SQLSTATE 28P01")
