"""`approve --ttl`: a bare number keeps meaning audit-clock ticks; a
duration (90s/30m/2h/1d) is wall-clock time (PANT-1119, GH #210)."""
import pytest

from portunus import broker as broker_mod
from portunus.cli import _parse_ttl, main


@pytest.fixture(autouse=True)
def _local_backend(monkeypatch):
    monkeypatch.delenv("PORTUNUS_BACKEND", raising=False)


@pytest.mark.parametrize("raw,expected", [
    ("3", ("ticks", 3)),
    ("90s", ("seconds", 90)),
    ("30m", ("seconds", 1800)),
    ("2h", ("seconds", 7200)),
    ("1D", ("seconds", 86400)),
])
def test_parse_ttl(raw, expected):
    assert _parse_ttl(raw) == expected


@pytest.mark.parametrize("raw", ["", "h", "2w", "1.5h", "-2h"])
def test_parse_ttl_rejects(raw):
    with pytest.raises(ValueError):
        _parse_ttl(raw)


def _gated_ref(home):
    value_file = home / "v.txt"
    value_file.write_text("x-value\n")
    assert main(["drop", "sudo-pw", "dostal-sudo", "--value-file", str(value_file)]) == 0
    assert main(["state", "sudo-pw", "enabled"]) == 0
    assert main(["gate", "sudo-pw"]) == 0


def test_wall_clock_ttl_outlives_many_accesses_then_expires(home, monkeypatch, capsys):
    _gated_ref(home)
    now = [1_000_000.0]
    monkeypatch.setattr(broker_mod.time, "time", lambda: now[0])
    assert main(["approve", "sudo-pw", "--ttl", "2h"]) == 0
    assert "2h" in capsys.readouterr().out

    from portunus.cli import _build
    _, _, broker, _ = _build()
    for _ in range(10):  # far more than the 3-tick default would allow
        broker.check_injectable("sudo-pw")

    now[0] += 7201
    with pytest.raises(broker_mod.ApprovalRequired):
        broker.check_injectable("sudo-pw")


def test_tick_ttl_still_works(home, capsys):
    _gated_ref(home)
    assert main(["approve", "sudo-pw", "--ttl", "1"]) == 0
    assert "1 access" in capsys.readouterr().out


def test_bad_ttl_is_an_error(home, capsys):
    _gated_ref(home)
    assert main(["approve", "sudo-pw", "--ttl", "soon"]) == 1
    assert "invalid --ttl" in capsys.readouterr().err
    assert main(["approve", "sudo-pw", "--ttl", "0h"]) == 1
