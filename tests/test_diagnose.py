"""Reverse rotation diagnosis (GitHub #208): audit entries carry a hashed
wall-clock ``ts``, `portunus audit` filters by time window and project, and
`portunus diagnose` ranks what changed in a window by proximity to the
project that broke."""
import json
from datetime import datetime, timezone

import pytest

from portunus import audit as audit_mod
from portunus import diagnose
from portunus.audit import AuditChain
from portunus.cli import main
from portunus.registry import Registry


def _epoch(text):
    return int(datetime.fromisoformat(text).replace(tzinfo=timezone.utc).timestamp())


@pytest.fixture
def clock(monkeypatch):
    """Pin the audit wall clock; set `clock.now` before each append."""
    class Clock:
        now = _epoch("2026-10-01T00:00:00")
    c = Clock()
    monkeypatch.setattr(audit_mod, "_now", lambda: c.now)
    return c


def _log(clock, chain, when, action, secret, result="ok"):
    clock.now = _epoch(when)
    return chain.append(action, secret, result)


# --- the timestamp in the chain ------------------------------------------

def test_append_records_a_hashed_ts(home, clock):
    chain = AuditChain()
    e = _log(clock, chain, "2026-10-14T12:00:00", "rotate", "sm-a")
    assert e["ts"] == _epoch("2026-10-14T12:00:00")
    assert chain.verify()


def test_editing_ts_breaks_the_chain(home, clock):
    chain = AuditChain()
    _log(clock, chain, "2026-10-14T12:00:00", "rotate", "sm-a")
    line = json.loads(chain.path.read_text())
    line["ts"] += 86400
    chain.path.write_text(json.dumps(line, separators=(",", ":")) + "\n")
    assert not chain.verify()


def test_stripping_ts_breaks_the_chain(home, clock):
    chain = AuditChain()
    _log(clock, chain, "2026-10-14T12:00:00", "rotate", "sm-a")
    line = json.loads(chain.path.read_text())
    del line["ts"]
    chain.path.write_text(json.dumps(line, separators=(",", ":")) + "\n")
    assert not chain.verify()


def test_pre_timestamp_entries_still_verify_and_chain_onward(home, clock):
    """A log written before `ts` existed keeps verifying, and new
    timestamped entries chain onto it."""
    import hashlib
    body = json.dumps({"seq": 1, "actor": "u", "task": "", "action": "resolve",
                       "secret": "sm-a", "result": "ok", "prev": "genesis"},
                      separators=(",", ":"))
    legacy = json.loads(body)
    legacy["h"] = hashlib.sha256(("genesis" + body).encode()).hexdigest()
    chain = AuditChain()
    chain.path.write_text(json.dumps(legacy, separators=(",", ":")) + "\n")
    assert chain.verify()
    _log(clock, chain, "2026-10-14T12:00:00", "rotate", "sm-a")
    assert chain.verify()
    assert chain.check()["entries"] == 2


# --- time parsing ----------------------------------------------------------

def test_parse_when_forms():
    now = _epoch("2026-10-17T00:00:00")
    assert diagnose.parse_when("2026-10-15") == _epoch("2026-10-15T00:00:00")
    assert diagnose.parse_when("2026-10-15", end_of_day=True) == _epoch("2026-10-15T23:59:59")
    assert diagnose.parse_when("2026-10-15T09:30") == _epoch("2026-10-15T09:30:00")
    assert diagnose.parse_when("2026-10-15T09:30:00Z") == _epoch("2026-10-15T09:30:00")
    assert diagnose.parse_when("36h", now=now) == now - 36 * 3600
    assert diagnose.parse_when("2w", now=now) == now - 14 * 86400
    assert diagnose.parse_when("1760000000") == 1760000000
    with pytest.raises(ValueError):
        diagnose.parse_when("mid-october")


# --- portunus audit --since/--until/--project/--changes -------------------

def test_audit_filters_by_time_window_without_a_secret_name(home, clock, capsys):
    chain = AuditChain()
    _log(clock, chain, "2026-10-01T00:00:00", "rotate", "sm-early")
    _log(clock, chain, "2026-10-14T00:00:00", "rotate", "sm-mid")
    _log(clock, chain, "2026-10-15T23:00:00", "drop", "sm-mid2", "stored")
    _log(clock, chain, "2026-10-20T00:00:00", "rotate", "sm-late")

    rc = main(["audit", "--json", "--since", "2026-10-12", "--until", "2026-10-15"])
    assert rc == 0
    names = [e["secret"] for e in json.loads(capsys.readouterr().out)]
    assert names == ["sm-mid", "sm-mid2"]  # --until date is inclusive of the day


def test_audit_window_is_not_truncated_to_the_default_tail(home, clock, capsys):
    chain = AuditChain()
    for i in range(30):
        _log(clock, chain, f"2026-10-14T00:{i:02d}:00", "resolve", f"sm-{i}")
    main(["audit", "--json", "--since", "2026-10-14"])
    assert len(json.loads(capsys.readouterr().out)) == 30
    main(["audit", "5", "--json", "--since", "2026-10-14"])
    assert len(json.loads(capsys.readouterr().out)) == 5


def test_audit_changes_and_project_filters(home, clock, capsys):
    reg = Registry()
    reg.add("shop-db", "sm-shop-db", project="shop.mdostal.com")
    reg.add("other-db", "sm-other-db", project="elsewhere")
    chain = AuditChain()
    _log(clock, chain, "2026-10-14T00:00:00", "resolve", "sm-shop-db")
    _log(clock, chain, "2026-10-14T01:00:00", "rotate:neon-role-password", "sm-shop-db")
    _log(clock, chain, "2026-10-14T02:00:00", "rotate", "sm-other-db")

    main(["audit", "--json", "--changes", "--project", "shop.mdostal.com", "--since", "2026-10-13"])
    entries = json.loads(capsys.readouterr().out)
    assert [(e["action"], e["secret"]) for e in entries] == [
        ("rotate:neon-role-password", "sm-shop-db")]


def test_audit_window_notes_untimed_entries(home, clock, capsys):
    chain = AuditChain()
    chain.path.write_text(json.dumps({"seq": 1, "action": "rotate", "secret": "x"}) + "\n")
    main(["audit", "--since", "2026-10-01"])
    assert "predate timestamps" in capsys.readouterr().err


def test_audit_rejects_bad_window(home, capsys):
    assert main(["audit", "--since", "2026-10-15", "--until", "2026-10-01"]) == 1
    assert main(["audit", "--since", "whenever"]) == 1


def test_audit_text_shows_when(home, clock, capsys):
    chain = AuditChain()
    _log(clock, chain, "2026-10-14T12:34:00", "rotate", "sm-a")
    main(["audit"])
    assert "2026-10-14T12:34Z" in capsys.readouterr().out


# --- ranking ---------------------------------------------------------------

def _graph():
    reg = Registry()
    reg.add("shop-db", "sm-shop-db", project="shop.mdostal.com", provider="neon",
            injected_as={"DATABASE_URL": "env:DATABASE_URL"})
    reg.add("shop-stripe", "sm-shop-stripe", group="shop.mdostal.com/payments")
    reg.add("neon-api", "sm-neon-api", project="infra", related=["shop-db"])
    reg.add("shop-cdn", "sm-shop-cdn", project="cdn", group="shop.mdostal.com/cdn")
    reg.add("blog-key", "sm-blog-key", project="blog")
    return reg


def test_diagnose_ranks_project_then_related_then_elsewhere(home, clock):
    reg = _graph()
    chain = AuditChain()
    _log(clock, chain, "2026-10-10T00:00:00", "rotate", "sm-blog-key")
    _log(clock, chain, "2026-10-10T00:00:00", "rotate", "sm-blog-key")
    _log(clock, chain, "2026-10-11T00:00:00", "rotate", "sm-neon-api")
    _log(clock, chain, "2026-10-12T00:00:00", "drop", "sm-shop-db", "stored")
    _log(clock, chain, "2026-10-12T00:00:00", "resolve", "sm-shop-stripe")  # read, not a change
    _log(clock, chain, "2026-10-13T00:00:00", "oauth_store", "gone-key", "stored")

    report = diagnose.diagnose(chain.entries(), reg, _epoch("2026-10-09T00:00:00"), None,
                               project="shop.mdostal.com")
    order = [(c["reference"] or c["audit_name"], c["tier"]) for c in report["candidates"]]
    assert order == [
        ("shop-db", "project"),
        ("neon-api", "related"),
        ("blog-key", "elsewhere"),   # 2 changes outranks 1 within a tier
        ("gone-key", "elsewhere"),
    ]
    assert report["candidates"][0]["injected_as"] == {"DATABASE_URL": "env:DATABASE_URL"}


def test_group_root_counts_as_the_project(home):
    reg = _graph()
    tiers = diagnose._tiers(list(reg), "shop.mdostal.com")
    assert tiers["shop-stripe"] == "project"
    assert tiers["shop-cdn"] == "project"
    assert tiers["blog-key"] == "elsewhere"


def test_diagnose_include_all_keeps_reads(home, clock):
    reg = _graph()
    chain = AuditChain()
    _log(clock, chain, "2026-10-12T00:00:00", "resolve", "sm-shop-stripe")
    report = diagnose.diagnose(chain.entries(), reg, _epoch("2026-10-01T00:00:00"), None,
                               include_all=True)
    assert [c["reference"] for c in report["candidates"]] == ["shop-stripe"]


# --- portunus diagnose CLI -----------------------------------------------

def test_diagnose_cli_requires_since(home, capsys):
    assert main(["diagnose"]) == 1


def test_diagnose_cli_text_report(home, clock, capsys):
    _graph()
    chain = AuditChain()
    _log(clock, chain, "2026-10-12T08:00:00", "rotate:neon-role-password", "sm-shop-db", "ok:new=r2")
    _log(clock, chain, "2026-10-12T09:00:00", "rotate", "sm-blog-key")
    rc = main(["diagnose", "--since", "2026-10-10", "--until", "2026-10-16",
               "--project", "shop.mdostal.com"])
    out = capsys.readouterr().out
    assert rc == 0
    assert out.index("shop-db") < out.index("blog-key")
    assert "in shop.mdostal.com:" in out
    assert "rotate:neon-role-password" in out
    assert "--check" in out


def test_diagnose_cli_empty_window(home, clock, capsys):
    _graph()
    assert main(["diagnose", "--since", "2026-10-10"]) == 0
    assert "no changes recorded" in capsys.readouterr().out


def test_diagnose_check_reverifies_near_candidates_and_points_at_injection_targets(
        home, clock, capsys, monkeypatch):
    from portunus import liveness
    _graph()
    chain = AuditChain()
    _log(clock, chain, "2026-10-12T08:00:00", "rotate", "sm-shop-db")
    _log(clock, chain, "2026-10-12T09:00:00", "rotate", "sm-blog-key")

    checked = []

    def fake_check(ref, resolver, adapter=None):
        checked.append(ref.name)
        return liveness.LivenessResult(ref.name, ref.provider, liveness.DEAD, 401, "rejected")

    monkeypatch.setattr(liveness, "check_reference", fake_check)
    clock.now = _epoch("2026-10-17T00:00:00")
    rc = main(["diagnose", "--since", "2026-10-10", "--project", "shop.mdostal.com", "--check"])
    out = capsys.readouterr().out
    assert rc == 2
    assert checked == ["shop-db"]  # elsewhere-tier candidates are not probed
    assert "liveness now: DEAD" in out
    assert "re-inject once fixed -> DATABASE_URL=env:DATABASE_URL" in out
    # the re-check itself lands in the audit log
    assert any(e["action"] == "liveness" for e in AuditChain().entries())


def test_diagnose_json_never_contains_a_value(home, clock, capsys):
    reg = _graph()
    chain = AuditChain()
    _log(clock, chain, "2026-10-12T08:00:00", "drop", "sm-shop-db", "stored")
    main(["diagnose", "--since", "2026-10-10", "--json", "--project", "shop.mdostal.com"])
    report = json.loads(capsys.readouterr().out)
    assert report["candidates"][0]["reference"] == "shop-db"
    assert set(report["candidates"][0]["events"][0]) == {"seq", "ts", "actor", "action", "result"}
