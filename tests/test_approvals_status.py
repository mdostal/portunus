"""GH #257 / PANT-1401: the gate status the vault UI shows, and the CLI
fixes that came with it.

- ``Broker.approval_status`` / ``portunus approvals``: gated or not, whether
  an approval is live and its time box, and whether an access is waiting.
- A "not injectable" / "needs approval" error names the reference, not only
  the vault key.
- Re-dropping (or fulfilling) a gated reference keeps the gate.
"""
import json

import pytest

from portunus import broker as broker_mod
from portunus.cli import _build, main


@pytest.fixture(autouse=True)
def _local_backend(monkeypatch):
    monkeypatch.delenv("PORTUNUS_BACKEND", raising=False)


def _drop(home, name="card-1", sm_name="sm-card-1", enable=True):
    value_file = home / f"{name}.txt"
    value_file.write_text("x-value\n")
    assert main(["drop", name, sm_name, "--value-file", str(value_file)]) == 0
    if enable:
        assert main(["state", name, "enabled"]) == 0


def test_ungated_reference(home):
    _drop(home)
    _, _, broker, _ = _build()
    status = broker.approval_status("card-1")
    assert status["gated"] is False
    assert status["approved"] is False
    assert status["pending"] is False
    assert status["state"] == "enabled"


def test_gated_then_denied_access_is_pending(home):
    _drop(home)
    assert main(["gate", "card-1"]) == 0
    _, _, broker, _ = _build()
    assert broker.approval_status("card-1")["pending"] is False  # nobody asked yet

    with pytest.raises(broker_mod.ApprovalRequired):
        broker.check_injectable("card-1")
    status = broker.approval_status("card-1")
    assert status["gated"] is True
    assert status["pending"] is True
    assert status["last_requested_seq"] is not None


def test_wall_clock_approval_shows_time_box_and_clears_pending(home, monkeypatch):
    _drop(home)
    assert main(["gate", "card-1"]) == 0
    _, _, broker, _ = _build()
    with pytest.raises(broker_mod.ApprovalRequired):
        broker.check_injectable("card-1")

    now = [2_000_000.0]
    monkeypatch.setattr(broker_mod.time, "time", lambda: now[0])
    assert main(["approve", "card-1", "--ttl", "2h"]) == 0
    status = broker.approval_status("card-1")
    assert status["approved"] is True
    assert status["expires_at"] == 2_000_000 + 7200
    assert status["ticks_left"] is None
    assert status["pending"] is False
    assert status["approved_by"]

    now[0] += 7201
    status = broker.approval_status("card-1")
    assert status["approved"] is False
    assert status["expires_at"] is None
    # read-only: an expired token is not deleted by a status read
    assert (broker.approvals_dir / "card-1").exists()


def test_tick_approval_reports_ticks_left(home):
    _drop(home)
    assert main(["gate", "card-1"]) == 0
    assert main(["approve", "card-1", "--ttl", "3"]) == 0
    _, _, broker, _ = _build()
    status = broker.approval_status("card-1")
    assert status["approved"] is True
    assert status["ticks_left"] is not None and status["ticks_left"] >= 0
    assert status["expires_at"] is None


def test_gate_off_is_not_pending_even_after_a_denial(home):
    _drop(home)
    assert main(["gate", "card-1"]) == 0
    _, _, broker, _ = _build()
    with pytest.raises(broker_mod.ApprovalRequired):
        broker.check_injectable("card-1")
    assert main(["gate", "card-1", "--off"]) == 0
    _, _, broker, _ = _build()
    status = broker.approval_status("card-1")
    assert status["gated"] is False
    assert status["pending"] is False


def test_cli_approvals_json_lists_only_gated(home, capsys):
    _drop(home, "card-1", "sm-card-1")
    _drop(home, "plain", "sm-plain")
    assert main(["gate", "card-1"]) == 0
    capsys.readouterr()
    assert main(["approvals", "--json"]) == 0
    rows = json.loads(capsys.readouterr().out)
    assert [r["name"] for r in rows] == ["card-1"]


def test_cli_approvals_pending_filter_and_name(home, capsys):
    _drop(home, "card-1", "sm-card-1")
    _drop(home, "card-2", "sm-card-2")
    assert main(["gate", "card-1"]) == 0
    assert main(["gate", "card-2"]) == 0
    _, _, broker, _ = _build()
    with pytest.raises(broker_mod.ApprovalRequired):
        broker.check_injectable("card-2")
    capsys.readouterr()

    assert main(["approvals", "--pending", "--json"]) == 0
    assert [r["name"] for r in json.loads(capsys.readouterr().out)] == ["card-2"]

    _drop(home, "plain", "sm-plain")
    capsys.readouterr()
    assert main(["approvals", "--name", "plain", "--json"]) == 0
    rows = json.loads(capsys.readouterr().out)
    assert rows[0]["name"] == "plain" and rows[0]["gated"] is False

    assert main(["approvals", "--name", "nope"]) != 0


def test_cli_approvals_text(home, capsys):
    _drop(home)
    assert main(["gate", "card-1"]) == 0
    assert main(["approve", "card-1", "--ttl", "30m"]) == 0
    capsys.readouterr()
    assert main(["approvals"]) == 0
    out = capsys.readouterr().out
    assert "card-1" in out and "approved until" in out


def test_not_injectable_error_names_the_reference(home, capsys):
    _drop(home, "free-reign-token", "fr-vault-key", enable=False)
    capsys.readouterr()
    assert main(["resolve", "{{secret:free-reign-token}}"]) != 0
    err = capsys.readouterr().err
    assert "free-reign-token (fr-vault-key) is dropped" in err
    assert "portunus state free-reign-token enabled" in err


def test_approval_error_names_the_reference(home):
    _drop(home, "card-1", "sm-card-1")
    assert main(["gate", "card-1"]) == 0
    _, _, broker, _ = _build()
    with pytest.raises(broker_mod.ApprovalRequired, match=r"card-1 \(sm-card-1\) needs approval"):
        broker.check_injectable("card-1")


def test_redrop_keeps_the_gate(home):
    _drop(home)
    assert main(["gate", "card-1"]) == 0
    _drop(home)  # a rotation / fulfil over the same name
    registry, *_ = _build()
    assert registry.get("card-1").approval == "required"


def test_gate_off_still_clears(home):
    _drop(home)
    assert main(["gate", "card-1"]) == 0
    assert main(["gate", "card-1", "--off"]) == 0
    _drop(home)
    registry, *_ = _build()
    assert registry.get("card-1").approval == ""
