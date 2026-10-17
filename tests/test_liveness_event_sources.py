"""Liveness probes + provider backfill for event-api's third-party event
sources: Eventbrite, Ticketmaster, SeatGeek, Meetup (PANT-1126 / GH #203).

Each provider stores several credentials and only some can be sent on their
own; the rest must read unknown without a request, never a false dead."""
import importlib.util
from pathlib import Path

import pytest

from portunus import liveness
from portunus.liveness import (
    DEAD, LIVE, UNKNOWN, EventbriteLivenessAdapter, MeetupLivenessAdapter,
    SeatGeekLivenessAdapter, TicketmasterLivenessAdapter, check_reference,
)

SECRET = "evt-do-not-leak-0xC0FFEE"


class FakeTransport:
    def __init__(self, code=200):
        self.code = code
        self.calls = []

    def __call__(self, method, url, headers, timeout):
        self.calls.append((method, url, dict(headers)))
        return self.code


def _add(stack, name, provider, tags=None):
    stack["registry"].add(name, f"sm-{name}", provider=provider, tags=tags)
    stack["backend"].set(f"sm-{name}", SECRET)
    return stack["registry"].get(name)


def test_all_four_registered():
    for provider, cls in (("eventbrite", EventbriteLivenessAdapter),
                          ("ticketmaster", TicketmasterLivenessAdapter),
                          ("seatgeek", SeatGeekLivenessAdapter),
                          ("meetup", MeetupLivenessAdapter)):
        assert isinstance(liveness.liveness_adapter_for(provider), cls)


# --- Eventbrite ------------------------------------------------------------

def test_eventbrite_private_token_is_a_bearer_probe(stack):
    ref = _add(stack, "event-api-prod-eventbrite-private-token", "eventbrite")
    t = FakeTransport(200)
    result = check_reference(ref, stack["resolver"], adapter=EventbriteLivenessAdapter(transport=t))
    assert result.status == LIVE
    assert t.calls == [("GET", "https://www.eventbriteapi.com/v3/users/me/",
                        {"Authorization": f"Bearer {SECRET}"})]
    assert SECRET not in repr(result)


@pytest.mark.parametrize("code,expected", [(401, DEAD), (403, UNKNOWN), (429, UNKNOWN), (500, UNKNOWN)])
def test_eventbrite_only_401_is_dead(stack, code, expected):
    ref = _add(stack, "event-api-dev-eventbrite-private-token", "eventbrite")
    adapter = EventbriteLivenessAdapter(transport=FakeTransport(code))
    assert check_reference(ref, stack["resolver"], adapter=adapter).status == expected


@pytest.mark.parametrize("suffix", ["api-key", "oauth-client-id", "oauth-client-secret"])
def test_eventbrite_non_bearer_credentials_are_never_sent(stack, suffix):
    ref = _add(stack, f"event-api-prod-eventbrite-{suffix}", "eventbrite")
    t = FakeTransport(401)
    result = check_reference(ref, stack["resolver"], adapter=EventbriteLivenessAdapter(transport=t))
    assert result.status == UNKNOWN and result.detail.startswith("not probed:")
    assert t.calls == []


def test_liveness_kind_tag_overrides_the_name(stack):
    # An `api-key` that is really a personal token can opt in explicitly.
    ref = _add(stack, "event-api-prod-eventbrite-api-key", "eventbrite",
               tags={"liveness_kind": "private-token"})
    t = FakeTransport(200)
    assert check_reference(ref, stack["resolver"], adapter=EventbriteLivenessAdapter(transport=t)).status == LIVE
    assert len(t.calls) == 1


# --- Ticketmaster ----------------------------------------------------------

def test_ticketmaster_consumer_key_goes_in_the_query_never_the_detail(stack):
    ref = _add(stack, "event-api-prod-ticketmaster-consumer-key", "ticketmaster")
    t = FakeTransport(401)
    result = check_reference(ref, stack["resolver"], adapter=TicketmasterLivenessAdapter(transport=t))
    assert result.status == DEAD and result.http_status == 401
    method, url, headers = t.calls[0]
    assert url == ("https://app.ticketmaster.com/discovery/v2/classifications.json"
                   f"?size=1&apikey={SECRET}")
    assert headers == {}
    assert SECRET not in result.detail and SECRET not in repr(result)


def test_ticketmaster_403_is_unknown(stack):
    ref = _add(stack, "event-api-prod-ticketmaster-consumer-key", "ticketmaster")
    adapter = TicketmasterLivenessAdapter(transport=FakeTransport(403))
    assert check_reference(ref, stack["resolver"], adapter=adapter).status == UNKNOWN


@pytest.mark.parametrize("suffix", ["consumer-secret", "callback-url"])
def test_ticketmaster_secret_and_config_not_probed(stack, suffix):
    ref = _add(stack, f"event-api-prod-ticketmaster-{suffix}", "ticketmaster")
    t = FakeTransport(200)
    result = check_reference(ref, stack["resolver"], adapter=TicketmasterLivenessAdapter(transport=t))
    assert result.status == UNKNOWN and t.calls == []


# --- SeatGeek --------------------------------------------------------------

def test_seatgeek_client_id_probe(stack):
    ref = _add(stack, "event-api-dev-seatgeek-client-id", "seatgeek")
    t = FakeTransport(403)
    result = check_reference(ref, stack["resolver"], adapter=SeatGeekLivenessAdapter(transport=t))
    assert result.status == DEAD
    assert t.calls[0][1] == f"https://api.seatgeek.com/2/taxonomies?client_id={SECRET}"
    assert SECRET not in result.detail


def test_seatgeek_client_secret_not_probed(stack):
    ref = _add(stack, "event-api-dev-seatgeek-client-secret", "seatgeek")
    t = FakeTransport(200)
    assert check_reference(ref, stack["resolver"], adapter=SeatGeekLivenessAdapter(transport=t)).status == UNKNOWN
    assert t.calls == []


# --- Meetup ----------------------------------------------------------------

@pytest.mark.parametrize("suffix,reason", [
    ("key", "retired API-key auth"), ("client-id", "OAuth"), ("client-secret", "OAuth")])
def test_meetup_never_probes_and_says_why(stack, suffix, reason):
    ref = _add(stack, f"event-api-prod-meetup-{suffix}", "meetup")
    t = FakeTransport(200)
    result = check_reference(ref, stack["resolver"], adapter=MeetupLivenessAdapter(transport=t))
    assert result.status == UNKNOWN and reason in result.detail
    assert t.calls == []


# --- sweep selection -------------------------------------------------------

def test_sweep_includes_only_probeable_kinds(stack):
    for name, provider in (
        ("event-api-prod-eventbrite-private-token", "eventbrite"),
        ("event-api-prod-eventbrite-oauth-client-secret", "eventbrite"),
        ("event-api-prod-ticketmaster-consumer-key", "ticketmaster"),
        ("event-api-prod-ticketmaster-consumer-secret", "ticketmaster"),
        ("event-api-prod-seatgeek-client-id", "seatgeek"),
        ("event-api-prod-seatgeek-client-secret", "seatgeek"),
        ("event-api-prod-meetup-key", "meetup"),
        ("event-api-prod-meetup-client-secret", "meetup"),
    ):
        _add(stack, name, provider)
    assert sorted(r.name for r in liveness.checkable_references(stack["registry"])) == [
        "event-api-prod-eventbrite-private-token",
        "event-api-prod-seatgeek-client-id",
        "event-api-prod-ticketmaster-consumer-key",
    ]


# --- provider backfill script ----------------------------------------------

def _load_backfill():
    path = Path(__file__).resolve().parents[1] / "scripts" / "backfill_event_api_providers.py"
    spec = importlib.util.spec_from_file_location("backfill_event_api_providers", path)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def test_backfill_sets_provider_only_and_is_idempotent(stack, capsys):
    backfill = _load_backfill()
    reg = stack["registry"]
    reg.add("event-api-prod-meetup-key", "sm-a", provider="gcp", tags={"note": "keep"})
    reg.add("event-api-dev-eventbrite-private-token", "sm-b", provider="gcp")
    reg.add("event-api-prod-ticketmaster-callback-url", "sm-c", provider="gcp")
    reg.add("event-api-prod-seatgeek-client-id", "sm-d", provider="seatgeek")   # already right
    reg.add("event-api-prod-stripe-key", "sm-e", provider="gcp")                # not ours
    reg.add("social-engine-meetup-key", "sm-f", provider="gcp")                 # not event-api

    assert backfill.main([]) == 0          # dry run writes nothing
    assert reg.get("event-api-prod-meetup-key").provider == "gcp"
    assert "3 reference(s)" in capsys.readouterr().out

    assert backfill.main(["--apply"]) == 0
    from portunus import Registry
    fresh = Registry()
    assert fresh.get("event-api-prod-meetup-key").provider == "meetup"
    assert fresh.get("event-api-prod-meetup-key").tags == {"note": "keep"}
    assert fresh.get("event-api-dev-eventbrite-private-token").provider == "eventbrite"
    assert fresh.get("event-api-prod-ticketmaster-callback-url").provider == "ticketmaster"
    assert fresh.get("event-api-prod-stripe-key").provider == "gcp"
    assert fresh.get("social-engine-meetup-key").provider == "gcp"
    assert backfill.plan(fresh) == []
