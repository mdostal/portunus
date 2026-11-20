"""Approval windows (GH #251): an agent asks, a human approves with a factor
the agent cannot reach, exactly one resolve gets through, and the audit
log says who asked, who approved and how."""
import subprocess
import time

import pytest

from portunus import approval as approval_mod
from portunus import prompt as prompt_mod
from portunus.broker import ApprovalRequired
from portunus.cli import main

SECRET = "FAKE-CARD-JSON-do-not-leak"
NAME = "visa-work"


@pytest.fixture(autouse=True)
def _local_backend(monkeypatch):
    monkeypatch.delenv("PORTUNUS_BACKEND", raising=False)


@pytest.fixture
def gated(stack):
    stack["registry"].add(NAME, "dostal-visa-work", scope="shared", kind="card")
    stack["backend"].set("dostal-visa-work", SECRET)
    stack["broker"].gate(NAME, on=True)
    return stack


class Factor:
    """An enrolled TOTP factor plus a source of fresh codes. Enrolment uses
    the previous step's code, so the current and next steps stay usable."""

    def __init__(self):
        self.secret = approval_mod.new_secret()
        self.step = int(time.time()) // approval_mod.TOTP_STEP - 1
        approval_mod.enroll(self.secret, approval_mod.totp(self.secret, self.step))

    def wrong(self) -> str:
        """Six digits guaranteed not to match any step in the window."""
        now = int(time.time()) // approval_mod.TOTP_STEP
        live = {approval_mod.totp(self.secret, st) for st in range(now - 2, now + 3)}
        return next(c for c in (f"{i:06d}" for i in range(10)) if c not in live)

    def code(self) -> str:
        self.step += 1
        return approval_mod.totp(self.secret, self.step)


@pytest.fixture
def factor(home):
    return Factor()


def _resolve(stack):
    seen = {}
    stack["resolver"].resolve_call(f"{{{{secret:{NAME}}}}}", lambda s: seen.setdefault("v", s))
    return seen["v"]


def _results(stack, action):
    return [e["result"] for e in stack["audit"].entries() if e["action"] == action]


def _as_agent(monkeypatch, who="bot"):
    monkeypatch.setenv("DOSTAL_AGENT", who)


def _as_human(monkeypatch):
    monkeypatch.delenv("DOSTAL_AGENT", raising=False)


# --- self-approval is refused -----------------------------------------------

@pytest.mark.parametrize("marker", ["DOSTAL_AGENT", "CLAUDECODE", "T3CODE_THREAD_ID",
                                    "MULTICA_AGENT_ID"])
def test_agent_cannot_approve_itself(gated, monkeypatch, capsys, marker):
    monkeypatch.setenv(marker, "1")
    assert main(["approve", NAME]) == 1
    assert "cannot approve its own access" in capsys.readouterr().err
    assert not (gated["broker"].approvals_dir / NAME).exists()
    assert any(r.startswith("refused:agent") and f"marker:{marker}" in r
               for r in _results(gated, "approve"))
    with pytest.raises(ApprovalRequired):
        _resolve(gated)


def test_agent_cannot_enroll_or_remove_the_factor(home, monkeypatch, capsys):
    monkeypatch.setenv("CLAUDECODE", "1")
    assert main(["approval", "enroll"]) == 1
    assert main(["approval", "remove", "--code", "123456"]) == 1
    assert not approval_mod.factor_enrolled()


# --- the factor ---------------------------------------------------------------

def test_a_code_is_accepted_once(factor):
    code = factor.code()
    assert approval_mod.verify_code(code) is True
    assert approval_mod.verify_code(code) is False  # replay


def test_an_older_code_is_refused_after_a_newer_one(factor):
    old, new = factor.code(), factor.code()
    assert approval_mod.verify_code(new) is True
    assert approval_mod.verify_code(old) is False


def test_wrong_and_malformed_codes_are_refused(factor):
    assert approval_mod.verify_code(factor.wrong()) is False
    assert approval_mod.verify_code("12345") is False
    assert approval_mod.verify_code("abcdef") is False


def test_enrol_refuses_a_wrong_code(home):
    with pytest.raises(approval_mod.FactorError):
        approval_mod.enroll(approval_mod.new_secret(), "000000", now=0)
    assert not approval_mod.factor_enrolled()


def test_the_factor_secret_is_not_in_the_state_file(factor, home):
    state = (home / "approval-factor.json").read_text()
    assert factor.secret not in state
    assert oct((home / "approval-factor.json").stat().st_mode & 0o777) == "0o600"


# --- requests ---------------------------------------------------------------

def test_request_needs_a_purpose(gated, monkeypatch):
    _as_agent(monkeypatch)
    out = approval_mod.run_request(gated["registry"], gated["broker"], NAME, surface="cli")
    assert "purpose" in out["error"]


def test_ungated_reference_needs_no_approval(stack):
    stack["registry"].add("plain", "dostal-plain", scope="shared", kind="token")
    out = approval_mod.run_request(stack["registry"], stack["broker"], "plain",
                                   purpose="x", surface="cli")
    assert out["needed"] is False


def test_request_summary_shows_purpose_amount_and_merchant(gated, monkeypatch):
    _as_agent(monkeypatch)
    out = approval_mod.run_request(gated["registry"], gated["broker"], NAME,
                                   purpose="domain renewal", amount="$12.00",
                                   merchant="Porkbun", surface="cli")
    assert out["pending"] is True
    for part in ("bot", "$12.00", "Porkbun", "domain renewal", NAME):
        assert part in out["summary"]
    assert out["ask_human_to_run"] == f"portunus approve {NAME} --request {out['request_id']}"
    assert any(r.startswith(f"requested:{out['request_id']} requester:bot")
               for r in _results(gated, "approval_request"))


def test_human_cli_approval_allows_exactly_one_resolve_and_is_audited(
        gated, factor, monkeypatch, capsys):
    _as_agent(monkeypatch)
    out = approval_mod.run_request(gated["registry"], gated["broker"], NAME,
                                   purpose="renewal", amount="$12", merchant="Porkbun",
                                   surface="cli")
    rid = out["request_id"]
    with pytest.raises(ApprovalRequired):
        _resolve(gated)

    _as_human(monkeypatch)
    assert main(["approve", NAME, "--request", rid, "--code", factor.code()]) == 0
    assert "one resolve" in capsys.readouterr().out

    _as_agent(monkeypatch)
    assert _resolve(gated) == SECRET
    with pytest.raises(ApprovalRequired):
        _resolve(gated)

    approved = [r for r in _results(gated, "approve") if r.startswith("approved:")]
    assert len(approved) == 1
    for part in ("approved:tester", "requester:bot", "channel:cli", f"request:{rid}",
                 "factor:totp", "uses:1"):
        assert part in approved[0]
    assert approval_mod.load_request(rid).status == "approved"
    assert approval_mod.load_request(rid).approver == "tester"


def test_a_decided_request_cannot_be_approved_twice(gated, factor, monkeypatch):
    _as_agent(monkeypatch)
    rid = approval_mod.run_request(gated["registry"], gated["broker"], NAME,
                                   purpose="renewal", surface="cli")["request_id"]
    _as_human(monkeypatch)
    assert main(["approve", NAME, "--request", rid, "--code", factor.code()]) == 0
    assert main(["approve", NAME, "--request", rid, "--code", factor.code()]) == 1


def test_human_approval_refused_with_a_wrong_or_replayed_code(gated, factor, monkeypatch):
    _as_agent(monkeypatch)
    rid = approval_mod.run_request(gated["registry"], gated["broker"], NAME,
                                   purpose="renewal", surface="cli")["request_id"]
    _as_human(monkeypatch)
    code = factor.code()
    assert approval_mod.verify_code(code)  # someone already used this code
    assert main(["approve", NAME, "--request", rid, "--code", code]) == 1
    assert main(["approve", NAME, "--request", rid, "--code", factor.wrong()]) == 1
    assert not (gated["broker"].approvals_dir / NAME).exists()
    assert any(r.startswith("refused:bad-code") for r in _results(gated, "approve"))


def test_approval_is_bound_to_the_requester(gated, factor, monkeypatch):
    _as_agent(monkeypatch, "bot")
    rid = approval_mod.run_request(gated["registry"], gated["broker"], NAME,
                                   purpose="renewal", surface="cli")["request_id"]
    _as_human(monkeypatch)
    assert main(["approve", NAME, "--request", rid, "--code", factor.code()]) == 0

    _as_agent(monkeypatch, "eve")
    with pytest.raises(ApprovalRequired):
        _resolve(gated)
    assert "approval-not-for:eve" in _results(gated, "resolve")
    _as_agent(monkeypatch, "bot")  # the token was not spent by eve
    assert _resolve(gated) == SECRET


def test_non_consuming_checks_do_not_spend_a_single_use_approval(gated):
    gated["broker"].approve(NAME, ttl_seconds=600, once=True)
    with pytest.raises(ApprovalRequired):
        gated["broker"].check_injectable(NAME, consume=False)
    assert (gated["broker"].approvals_dir / NAME).exists()
    assert _resolve(gated) == SECRET
    with pytest.raises(ApprovalRequired):
        _resolve(gated)


def test_approve_once_without_a_request(gated, factor, monkeypatch):
    assert main(["approve", NAME, "--once", "--ttl", "10m", "--code", factor.code()]) == 0
    assert _resolve(gated) == SECRET
    with pytest.raises(ApprovalRequired):
        _resolve(gated)


def test_approve_without_a_factor_still_works_but_warns(gated, capsys):
    assert main(["approve", NAME, "--ttl", "2"]) == 0
    assert "no approval factor is enrolled" in capsys.readouterr().err
    assert _resolve(gated) == SECRET


def test_approve_with_a_factor_and_no_code_is_refused(gated, factor, monkeypatch):
    monkeypatch.setattr("sys.stdin.isatty", lambda: False, raising=False)
    assert main(["approve", NAME]) == 1
    with pytest.raises(ApprovalRequired):
        _resolve(gated)


def test_an_expired_request_cannot_be_approved(gated, factor, monkeypatch):
    _as_agent(monkeypatch)
    req = approval_mod.create_request(NAME, "renewal", now=time.time() - 2 * approval_mod.REQUEST_TTL)
    _as_human(monkeypatch)
    assert main(["approve", NAME, "--request", req.id, "--code", factor.code()]) == 1
    assert approval_mod.load_request(req.id).status == "expired"


# --- the t3 channel -----------------------------------------------------------

def test_t3_request_is_a_card_then_a_code_file(gated, factor, monkeypatch, tmp_path):
    _as_agent(monkeypatch)
    monkeypatch.setenv("T3CODE_THREAD_ID", "thread-1")
    first = approval_mod.run_request(gated["registry"], gated["broker"], NAME,
                                     purpose="renewal", amount="$12", merchant="Porkbun")
    assert first["surface"] == "t3" and first["pending"] is True
    card = first["card"]
    assert card["tool"] == "mcp__t3-code__request_secret"
    assert set(card["arguments"]) == {"label", "reason", "delivery"}
    assert "$12" in card["arguments"]["label"] and "Porkbun" in card["arguments"]["label"]
    assert first["then"]["arguments"]["request_id"] == first["request_id"]

    code_file = tmp_path / "code"
    code_file.write_text(factor.code() + "\n")
    code_file.chmod(0o600)
    second = approval_mod.run_request(gated["registry"], gated["broker"], NAME,
                                      request_id=first["request_id"], code_file=str(code_file))
    assert second["approved"] is True and second["channel"] == "t3" and second["uses"] == 1
    assert not code_file.exists()
    assert _resolve(gated) == SECRET
    with pytest.raises(ApprovalRequired):
        _resolve(gated)
    approved = [r for r in _results(gated, "approve") if r.startswith("approved:")]
    assert "channel:t3" in approved[0] and "requester:bot" in approved[0]


def test_t3_wrong_code_approves_nothing_and_three_strikes_deny(gated, factor, monkeypatch,
                                                              tmp_path):
    _as_agent(monkeypatch)
    rid = approval_mod.run_request(gated["registry"], gated["broker"], NAME,
                                   purpose="renewal", surface="t3")["request_id"]
    for left in (2, 1, None):
        f = tmp_path / "code"
        f.write_text(factor.wrong())
        f.chmod(0o600)
        out = approval_mod.run_request(gated["registry"], gated["broker"], NAME,
                                       request_id=rid, code_file=str(f))
        assert out["approved"] is False
        assert out.get("tries_left") == left
    assert approval_mod.load_request(rid).status == "denied"
    assert not (gated["broker"].approvals_dir / NAME).exists()


def test_t3_without_a_factor_falls_back_to_the_terminal(gated, monkeypatch):
    _as_agent(monkeypatch)
    out = approval_mod.run_request(gated["registry"], gated["broker"], NAME,
                                   purpose="renewal", surface="t3")
    assert out["surface"] == "cli" and "approval enroll" in out["warning"]
    assert "card" not in out


# --- the dialog channel -------------------------------------------------------

class FakeRunner:
    def __init__(self, *answers):
        self.answers = list(answers)
        self.calls = []

    def __call__(self, argv, **kwargs):
        self.calls.append(argv)
        rc, out = self.answers.pop(0)
        return subprocess.CompletedProcess(argv, rc, stdout=out, stderr="")


def test_dialog_approval(gated, factor, monkeypatch):
    _as_agent(monkeypatch)
    runner = FakeRunner((0, factor.code() + "\n"))
    out = approval_mod.run_request(gated["registry"], gated["broker"], NAME,
                                   purpose="renewal", amount="$12", merchant="Porkbun",
                                   surface="zenity", runner=runner, which=lambda b: "/bin/" + b)
    assert out["approved"] is True and out["channel"] == "dialog:zenity"
    assert any("Porkbun" in arg for arg in runner.calls[0])
    assert _resolve(gated) == SECRET
    with pytest.raises(ApprovalRequired):
        _resolve(gated)


def test_dialog_cancel_denies(gated, factor, monkeypatch):
    _as_agent(monkeypatch)
    out = approval_mod.run_request(gated["registry"], gated["broker"], NAME,
                                   purpose="renewal", surface="zenity",
                                   runner=FakeRunner((1, "")), which=lambda b: "/bin/" + b)
    assert out["denied"] is True
    assert approval_mod.load_request(out["request_id"]).status == "denied"
    with pytest.raises(ApprovalRequired):
        _resolve(gated)


# --- surfaces -----------------------------------------------------------------

def test_cli_request_approval_and_status(gated, monkeypatch, capsys):
    _as_agent(monkeypatch)
    assert main(["request-approval", NAME, "--purpose", "renewal", "--surface", "cli"]) == 0
    import json
    out = json.loads(capsys.readouterr().out)
    assert out["pending"] is True
    _as_human(monkeypatch)
    assert main(["approval", "status", "--json"]) == 0
    status = json.loads(capsys.readouterr().out)
    assert [p["request_id"] for p in status["pending"]] == [out["request_id"]]


def test_mcp_request_approval_tool(gated, monkeypatch):
    from portunus import mcp_server

    _as_agent(monkeypatch)
    out = mcp_server.portunus_request_approval(NAME, purpose="renewal", surface="cli")
    assert out["pending"] is True and "ask_human_to_run" in out
    assert SECRET not in repr(out)
