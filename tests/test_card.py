"""`--kind card` (GH #251): a payment card asked for as fields, checked
(Luhn, expiry in the future, CVC length), stored as ONE reference and shown
everywhere as brand + last four only."""
import json
import subprocess
from datetime import date

import pytest

from portunus import card as card_mod
from portunus import prompt as prompt_mod
from portunus.audit import AuditChain
from portunus.cli import main
from portunus.localvault import LocalEncryptedBackend
from portunus.registry import Registry

NUMBER = "4242 4242 4242 4242"
DIGITS = "4242424242424242"
CVC = "987"
NEXT_YEAR = date.today().year + 1
EXPIRY = f"11/{NEXT_YEAR % 100:02d}"
TODAY = date(2026, 11, 20)


@pytest.fixture(autouse=True)
def _local_backend(monkeypatch):
    monkeypatch.delenv("PORTUNUS_BACKEND", raising=False)


class FakeRunner:
    def __init__(self, *answers):
        self.answers = list(answers)
        self.calls = []

    def __call__(self, argv, **kwargs):
        self.calls.append(argv)
        rc, out = self.answers.pop(0)
        return subprocess.CompletedProcess(argv, rc, stdout=out, stderr="")


def _use(monkeypatch, runner, surface):
    monkeypatch.setattr(prompt_mod.subprocess, "run", runner)
    monkeypatch.setattr(prompt_mod, "available_surfaces", lambda **kw: [surface])


def _run(capsys, *argv):
    rc = main(["prompt", *argv])
    captured = capsys.readouterr()
    return rc, json.loads(captured.out), captured


def _assert_no_leak(home, captured):
    for needle in (DIGITS, "4242 4242", CVC):
        assert needle not in captured.out
        assert needle not in captured.err
    for path in home.rglob("*"):
        if path.is_file() and path.name not in ("vault", "master.key"):
            data = path.read_bytes()
            assert DIGITS.encode() not in data, path
            assert f'"cvc":"{CVC}"'.encode() not in data, path


# --- validation -------------------------------------------------------------

@pytest.mark.parametrize("number,brand", [
    ("4242424242424242", "visa"),
    ("5555555555554444", "mastercard"),
    ("2223003122003222", "mastercard"),
    ("378282246310005", "amex"),
    ("6011111111111117", "discover"),
    ("3566002020360505", "jcb"),
    ("36227206271667", "diners"),
])
def test_brand_and_luhn(number, brand):
    assert card_mod.luhn_ok(number)
    assert card_mod.brand_of(number) == brand


def test_luhn_rejects_a_typo_without_echoing_the_number():
    with pytest.raises(card_mod.CardError) as exc:
        card_mod.normalize_number("4242 4242 4242 4241")
    assert "Luhn" in str(exc.value)
    assert "4241" not in str(exc.value)


def test_number_accepts_spaces_and_dashes_only():
    assert card_mod.normalize_number("4242-4242 4242-4242") == DIGITS
    with pytest.raises(card_mod.CardError):
        card_mod.normalize_number("4242x4242424242424")


@pytest.mark.parametrize("raw,want", [
    ("12/30", (12, 2030)), ("1/2031", (1, 2031)), ("0628", (6, 2028)),
    ("2029-03", (3, 2029)), ("11/26", (11, 2026)),  # this month still passes
])
def test_expiry_formats(raw, want):
    assert card_mod.parse_expiry(raw, TODAY) == want


@pytest.mark.parametrize("raw", ["10/26", "13/30", "1230x", "", "12/99"])
def test_expiry_rejects_past_bad_or_absurd(raw):
    with pytest.raises(card_mod.CardError):
        card_mod.parse_expiry(raw, TODAY)


def test_cvc_length_depends_on_brand():
    assert card_mod.normalize_cvc("123", DIGITS) == "123"
    with pytest.raises(card_mod.CardError):
        card_mod.normalize_cvc("123", "378282246310005")
    assert card_mod.normalize_cvc("1234", "378282246310005") == "1234"


def test_from_fields_and_value_shape():
    card = card_mod.from_fields({"number": NUMBER, "expiry": "12/30", "cvc": CVC,
                                 "name": "  A   Person ", "postal": "94110"}, TODAY)
    assert json.loads(card.to_value()) == {
        "number": DIGITS, "exp_month": 12, "exp_year": 2030, "cvc": CVC,
        "name": "A Person", "postal": "94110"}
    assert card.meta() == {"brand": "visa", "last4": "4242"}
    assert card_mod.display(card.meta()) == "visa ••••4242"


def test_from_fields_reports_missing_and_unknown():
    with pytest.raises(card_mod.CardError, match="cvc"):
        card_mod.from_fields({"number": NUMBER, "expiry": "12/30"}, TODAY)
    with pytest.raises(card_mod.CardError, match="pin"):
        card_mod.from_fields({"number": NUMBER, "expiry": "12/30", "cvc": CVC,
                              "pin": "1"}, TODAY)


def test_parse_single_json_and_lines():
    assert card_mod.parse_single(
        json.dumps({"number": DIGITS, "exp_month": 12, "exp_year": 2030, "cvc": CVC})
    )["expiry"] == "12/2030"
    fields = card_mod.parse_single(f"number: {NUMBER}\nexpiry: 12/30\ncvc: {CVC}\n")
    assert card_mod.from_fields(fields, TODAY).last4 == "4242"


# --- desktop surfaces -------------------------------------------------------

def test_zenity_shows_one_form_and_stores_one_card_reference(home, monkeypatch, capsys):
    sep = prompt_mod._FORM_SEPARATOR
    runner = FakeRunner((0, sep.join([NUMBER, EXPIRY, CVC, "A Person", "94110"]) + "\n"))
    _use(monkeypatch, runner, "zenity")
    rc, result, captured = _run(capsys, "visa-work", "visa-work", "--kind", "card")
    assert rc == 0, result
    assert result["stored"] is True and result["kind"] == "card"
    assert result["card"] == "visa ••••4242"
    assert len(runner.calls) == 1
    argv = runner.calls[0]
    assert argv[:2] == ["zenity", "--forms"]
    # number and CVC are masked, the rest are plain entries
    assert argv.count("--add-password") == 2 and argv.count("--add-entry") == 3
    ref = Registry().require("visa-work")
    assert ref.kind == "card" and ref.card == {"brand": "visa", "last4": "4242"}
    stored = json.loads(LocalEncryptedBackend().access("visa-work"))
    assert stored["number"] == DIGITS and stored["cvc"] == CVC
    _assert_no_leak(home, captured)


def test_zenity_form_reopens_with_the_reason_after_a_bad_card(home, monkeypatch, capsys):
    sep = prompt_mod._FORM_SEPARATOR
    runner = FakeRunner(
        (0, sep.join(["4242 4242 4242 4241", EXPIRY, CVC, "", ""]) + "\n"),
        (0, sep.join([NUMBER, EXPIRY, CVC, "", ""]) + "\n"),
    )
    _use(monkeypatch, runner, "zenity")
    rc, result, _ = _run(capsys, "visa-work", "visa-work", "--kind", "card")
    assert rc == 0, result
    second_text = runner.calls[1][runner.calls[1].index("--text") + 1]
    assert "Luhn" in second_text and "4241" not in second_text


def test_zenity_gives_up_after_three_bad_cards(home, monkeypatch, capsys):
    sep = prompt_mod._FORM_SEPARATOR
    bad = (0, sep.join([NUMBER, "01/20", CVC, "", ""]) + "\n")
    _use(monkeypatch, FakeRunner(bad, bad, bad), "zenity")
    rc, result, _ = _run(capsys, "visa-work", "visa-work", "--kind", "card")
    assert rc == 1 and "expired" in result["error"]
    assert "visa-work" not in Registry()


def test_kdialog_asks_one_field_at_a_time(home, monkeypatch, capsys):
    runner = FakeRunner((0, "4242424242424241\n"), (0, NUMBER + "\n"), (0, EXPIRY + "\n"),
                        (0, CVC + "\n"), (0, "\n"), (0, "\n"))
    _use(monkeypatch, runner, "kdialog")
    rc, result, captured = _run(capsys, "visa-work", "visa-work", "--kind", "card")
    assert rc == 0, result
    assert result["card"] == "visa ••••4242"
    modes = [argv[3] for argv in runner.calls]
    # number asked twice (first was a typo), masked; expiry shown; cvc masked
    assert modes == ["--password", "--password", "--inputbox", "--password",
                     "--inputbox", "--inputbox"]
    assert "Not accepted" in runner.calls[1][4]
    assert "Step 1 of 5" in runner.calls[0][4]
    _assert_no_leak(home, captured)


def test_cancel_mid_sequence_stores_nothing(home, monkeypatch, capsys):
    _use(monkeypatch, FakeRunner((0, NUMBER + "\n"), (1, "")), "kdialog")
    rc, result, _ = _run(capsys, "visa-work", "visa-work", "--kind", "card")
    assert rc == 2 and result == {"cancelled": True}
    assert "visa-work" not in Registry()


def test_ui_surface_refuses_cards(home, capsys):
    rc, result, _ = _run(capsys, "visa-work", "visa-work", "--kind", "card", "--surface", "ui")
    assert rc == 1 and "card" in result["error"]


def test_tty_entry_card_mode(tmp_path, monkeypatch, capsys):
    answers = iter(["4242424242424241", NUMBER, CVC])
    plain = iter([EXPIRY, "", ""])
    monkeypatch.setattr(prompt_mod.getpass, "getpass", lambda prompt="": next(answers))
    monkeypatch.setattr("builtins.input", lambda prompt="": next(plain))
    fifo = tmp_path / "out"
    assert prompt_mod.tty_entry(str(fifo), "banner", card=True) == 0
    message = json.loads(fifo.read_text())
    assert message == {"fields": {"number": NUMBER, "expiry": EXPIRY, "cvc": CVC}}
    out = capsys.readouterr().out
    assert DIGITS not in out and "Luhn" in out


# --- t3 surface -------------------------------------------------------------

@pytest.fixture
def card_files(tmp_path_factory):
    directory = tmp_path_factory.mktemp("t3-card")

    def write(**fields):
        paths = {}
        for key, value in fields.items():
            path = directory / key
            path.write_text(value + "\n")
            path.chmod(0o600)
            paths[key] = str(path)
        return paths

    return write


def test_t3_handoff_lists_one_card_per_field(home, capsys):
    rc, result, _ = _run(capsys, "visa-work", "visa-work", "--kind", "card",
                         "--surface", "t3", "--description", "Work Visa")
    assert rc == 0 and result["stored"] is False and result["kind"] == "card"
    assert [c["field"] for c in result["cards"]] == list(card_mod.FIELD_KEYS)
    assert all(c["tool"] == prompt_mod.T3_CARD_TOOL for c in result["cards"])
    assert all(c["arguments"]["delivery"] == "file" for c in result["cards"])
    assert result["cards"][0]["arguments"]["label"] == "Work Visa: Card number"
    assert [c["required"] for c in result["cards"]] == [True, True, True, False, False]
    args = result["then"]["arguments"]
    assert args["kind"] == "card" and "value_file" not in args
    assert set(args["value_files"]) == set(card_mod.FIELD_KEYS)
    cli = result["then"]["cli"]
    assert "number=<file path returned by the card>" in cli
    assert "visa-work" not in Registry()


def test_t3_completion_stores_and_deletes_files(home, capsys, card_files):
    paths = card_files(number=NUMBER, expiry=EXPIRY, cvc=CVC, postal="94110")
    argv = ["visa-work", "visa-work", "--kind", "card", "--project", "acme"]
    for key, path in paths.items():
        argv += ["--value-file", f"{key}={path}"]
    rc, result, captured = _run(capsys, *argv)
    assert rc == 0, result
    assert result["card"] == "visa ••••4242" and result["file_removed"] is True
    assert result["surface"] == "t3"
    for path in paths.values():
        assert not __import__("os").path.exists(path)
    stored = json.loads(LocalEncryptedBackend().access("visa-work"))
    assert stored["postal"] == "94110" and "name" not in stored
    _assert_no_leak(home, captured)
    # list and status show brand + last four only
    assert main(["list", "--project", "acme"]) == 0
    listing = capsys.readouterr().out
    assert "visa ••••4242" in listing and DIGITS not in listing
    assert main(["status", "visa-work"]) == 0
    status = capsys.readouterr().out
    assert "card:          visa ••••4242" in status


def test_t3_invalid_card_is_not_stored_and_files_go(home, capsys, card_files):
    paths = card_files(number="4242424242424241", expiry=EXPIRY, cvc=CVC)
    argv = ["visa-work", "visa-work", "--kind", "card"]
    for key, path in paths.items():
        argv += ["--value-file", f"{key}={path}"]
    rc, result, captured = _run(capsys, *argv)
    assert rc == 1 and "Luhn" in result["error"] and result["files_removed"] is True
    assert "visa-work" not in Registry()
    assert AuditChain().entries() == []
    _assert_no_leak(home, captured)


def test_t3_single_file_with_every_field(home, capsys, card_files):
    paths = card_files(card=f"number: {NUMBER}\nexpiry: {EXPIRY}\ncvc: {CVC}")
    rc, result, _ = _run(capsys, "visa-work", "visa-work", "--kind", "card",
                         "--value-file", paths["card"])
    assert rc == 0, result
    assert result["card"] == "visa ••••4242"


def test_field_value_files_need_kind_card(home, capsys):
    rc = main(["prompt", "x", "x", "--value-file", "number=/tmp/whatever"])
    assert rc == 1
    assert "only for --kind card" in capsys.readouterr().err


def test_mcp_prompt_takes_value_files(home, card_files):
    from portunus import mcp_server

    paths = card_files(number=NUMBER, expiry=EXPIRY, cvc=CVC)
    out = mcp_server.portunus_prompt(name="visa-work", sm_name="visa-work", kind="card",
                                     project="acme", value_files=paths)
    assert out["stored"] is True and out["card"] == "visa ••••4242"
    listed = mcp_server.portunus_list("acme")
    [entry] = [r for r in listed if r["name"] == "visa-work"]
    assert entry["card"] == {"brand": "visa", "last4": "4242"}
    assert DIGITS not in json.dumps(listed)
