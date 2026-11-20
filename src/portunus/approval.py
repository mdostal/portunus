"""Approval windows (GH #251): an agent asks to use a gated reference, a
human approves with a factor the agent cannot reach, and exactly one
resolve goes through.

    agent:  portunus request-approval visa-work --purpose "domain renewal" \
                --amount '$12.00' --merchant Namecheap
            -> a pending request recording who asked and why, plus a
               handoff for the channel the human will answer on
    human:  types the 6-digit code from their authenticator app into the
            T3 Code card / desktop dialog, or runs
            `portunus approve visa-work --request <id>` at their terminal
    then:   ONE resolve of visa-work, by the requester, within the window

The factor is a TOTP authenticator (RFC 6238: HMAC-SHA1, 6 digits, 30 s)
enrolled with `portunus approval enroll`. Its secret is kept encrypted
under the vault's master key; a code is accepted once (the step it
belongs to is recorded) so a code an agent sees in passing cannot be
replayed.

What this does and does not stop: an agent that only uses Portunus
through its CLI and MCP tools cannot approve its own request -- it does
not have the code, and `portunus approve` refuses a caller whose
environment marks it as an agent. An agent that runs as the same OS user
with unrestricted file access could still read Portunus's state files
directly; that needs OS-level separation (a different user, a sandbox)
and is out of scope here, as docs/access-control.md says.
"""
from __future__ import annotations

import base64
import hashlib
import hmac
import json
import os
import re
import secrets
import struct
import time
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Dict, List, Mapping, Optional
from urllib.parse import quote

from .paths import home

# --- who is asking ----------------------------------------------------------

# Environment variables an agent runtime sets for the processes it starts.
# Best effort: a hostile agent can unset them, which is why approving also
# needs the factor once one is enrolled.
AGENT_MARKERS = (
    "DOSTAL_AGENT",           # Portunus's own agent identity (audit actor)
    "PORTUNUS_AGENT",
    "AI_AGENT",
    "CLAUDECODE",             # Claude Code
    "CLAUDE_CODE_ENTRYPOINT",
    "T3CODE_THREAD_ID",       # T3 Code
    "MULTICA_AGENT_ID",       # Multica agent runtime
)


def agent_marker(env: Optional[Mapping[str, str]] = None) -> str:
    """The first agent marker set in `env`, or "" for a human's shell."""
    env = os.environ if env is None else env
    for name in AGENT_MARKERS:
        if env.get(name):
            return name
    return ""


def requester_name(env: Optional[Mapping[str, str]] = None) -> str:
    """Who is asking, as recorded on a request and checked on resolve."""
    env = os.environ if env is None else env
    for name in ("DOSTAL_AGENT", "PORTUNUS_AGENT", "MULTICA_AGENT_NAME"):
        if env.get(name):
            return env[name]
    user = env.get("USER", "unknown")
    return f"agent:{user}" if agent_marker(env) else user


# --- TOTP factor ------------------------------------------------------------

TOTP_STEP = 30
TOTP_DIGITS = 6
TOTP_WINDOW = 1  # accept the previous / next step too (clock drift)
_FACTOR_KEY = "approval-totp"


class FactorError(RuntimeError):
    """Enrolling or checking the factor failed. Never contains a code."""


def totp(secret_b32: str, step: int) -> str:
    key = base64.b32decode(secret_b32.upper() + "=" * (-len(secret_b32) % 8))
    digest = hmac.new(key, struct.pack(">Q", step), hashlib.sha1).digest()
    offset = digest[-1] & 0x0F
    code = (struct.unpack(">I", digest[offset:offset + 4])[0] & 0x7FFFFFFF) % 10 ** TOTP_DIGITS
    return f"{code:0{TOTP_DIGITS}d}"


def _factor_vault():
    from .localvault import LocalEncryptedBackend

    return LocalEncryptedBackend(vault_path=home() / "approval-factor.enc.json")


def _state_path() -> Path:
    return home() / "approval-factor.json"


def _load_state() -> dict:
    try:
        return json.loads(_state_path().read_text())
    except (OSError, ValueError):
        return {}


def _save_state(state: dict) -> None:
    path = _state_path()
    tmp = path.with_suffix(".tmp")
    tmp.write_text(json.dumps(state))
    os.chmod(tmp, 0o600)
    os.replace(tmp, path)


def factor_enrolled() -> bool:
    return bool(_load_state().get("enrolled"))


def new_secret() -> str:
    return base64.b32encode(secrets.token_bytes(20)).decode().rstrip("=")


def provisioning_uri(secret_b32: str, account: str) -> str:
    return (f"otpauth://totp/Portunus:{quote(account)}?secret={secret_b32}"
            f"&issuer=Portunus&digits={TOTP_DIGITS}&period={TOTP_STEP}")


def _matching_step(secret_b32: str, code: str, now: float, after: int) -> Optional[int]:
    code = re.sub(r"\s", "", code or "")
    if not re.fullmatch(rf"\d{{{TOTP_DIGITS}}}", code):
        return None
    current = int(now) // TOTP_STEP
    for step in range(current - TOTP_WINDOW, current + TOTP_WINDOW + 1):
        if step > after and hmac.compare_digest(totp(secret_b32, step), code):
            return step
    return None


def enroll(secret_b32: str, code: str, now: Optional[float] = None) -> None:
    """Save `secret_b32` as the approval factor once the human has proved
    their app has it by typing a current code."""
    now = time.time() if now is None else now
    step = _matching_step(secret_b32, code, now, after=-1)
    if step is None:
        raise FactorError("that code does not match -- check the app's clock and try again")
    _factor_vault().store(_FACTOR_KEY, secret_b32)
    _save_state({"enrolled": True, "enrolled_at": int(now), "last_step": step})


def remove_factor() -> None:
    _factor_vault().remove(_FACTOR_KEY)
    _state_path().unlink(missing_ok=True)


def verify_code(code: str, now: Optional[float] = None) -> bool:
    """True once per valid code: the step it belongs to is recorded and an
    older-or-equal step is never accepted again."""
    state = _load_state()
    if not state.get("enrolled"):
        raise FactorError("no approval factor enrolled -- the human runs `portunus approval enroll`")
    now = time.time() if now is None else now
    secret_b32 = _factor_vault().access(_FACTOR_KEY)
    step = _matching_step(secret_b32, code, now, after=int(state.get("last_step", -1)))
    del secret_b32
    if step is None:
        return False
    state["last_step"] = step
    _save_state(state)
    return True


# --- requests ---------------------------------------------------------------

REQUEST_TTL = 15 * 60       # a pending request expires after this
WINDOW_SECONDS = 10 * 60    # an approved request must be used within this
TEXT_MAX = 120


class RequestError(RuntimeError):
    pass


@dataclass
class ApprovalRequest:
    id: str
    name: str
    requester: str
    marker: str
    purpose: str
    amount: str = ""
    merchant: str = ""
    created_at: int = 0
    expires_at: int = 0
    status: str = "pending"      # pending | approved | denied | expired
    approver: str = ""
    channel: str = ""
    decided_at: int = 0
    extra: Dict[str, str] = field(default_factory=dict)

    def summary(self) -> str:
        """One line for the human: who wants what, for what."""
        what = f"pay {self.amount} to {self.merchant}" if self.amount and self.merchant else (
            f"pay {self.amount}" if self.amount else (
                f"pay {self.merchant}" if self.merchant else f"use {self.name}"))
        line = f"{self.requester} asks to {what}"
        if self.purpose:
            line += f" for {self.purpose}"
        if what != f"use {self.name}":
            line += f" (with {self.name})"
        return line

    def to_dict(self) -> dict:
        return asdict(self)


def _requests_dir() -> Path:
    path = home() / "approval-requests"
    path.mkdir(exist_ok=True)
    try:
        path.chmod(0o700)
    except OSError:
        pass
    return path


def _clean(text: str, label: str) -> str:
    flat = " ".join((text or "").split())
    if len(flat) > TEXT_MAX:
        raise RequestError(f"{label} is longer than {TEXT_MAX} characters")
    return flat


def _write(req: ApprovalRequest) -> None:
    path = _requests_dir() / f"{req.id}.json"
    tmp = path.with_suffix(".tmp")
    tmp.write_text(json.dumps(req.to_dict()))
    os.chmod(tmp, 0o600)
    os.replace(tmp, path)


def create_request(name: str, purpose: str, amount: str = "", merchant: str = "",
                   env: Optional[Mapping[str, str]] = None,
                   now: Optional[float] = None) -> ApprovalRequest:
    now = int(time.time() if now is None else now)
    purpose = _clean(purpose, "purpose")
    if not purpose:
        raise RequestError("say what the approval is for (--purpose)")
    req = ApprovalRequest(
        id=secrets.token_hex(4), name=name, requester=requester_name(env),
        marker=agent_marker(env), purpose=purpose, amount=_clean(amount, "amount"),
        merchant=_clean(merchant, "merchant"), created_at=now, expires_at=now + REQUEST_TTL,
    )
    _write(req)
    return req


def load_request(request_id: str, now: Optional[float] = None) -> ApprovalRequest:
    if not re.fullmatch(r"[0-9a-f]{8}", request_id or ""):
        raise RequestError(f"no approval request {request_id!r}")
    try:
        raw = json.loads((_requests_dir() / f"{request_id}.json").read_text())
    except (OSError, ValueError):
        raise RequestError(f"no approval request {request_id!r}")
    req = ApprovalRequest(**raw)
    now = time.time() if now is None else now
    if req.status == "pending" and now > req.expires_at:
        req.status = "expired"
        _write(req)
    return req


def decide(req: ApprovalRequest, status: str, approver: str, channel: str,
           now: Optional[float] = None) -> ApprovalRequest:
    req.status = status
    req.approver = approver
    req.channel = channel
    req.decided_at = int(time.time() if now is None else now)
    _write(req)
    return req


def pending_requests(now: Optional[float] = None) -> List[ApprovalRequest]:
    out = []
    for path in sorted(_requests_dir().glob("*.json")):
        try:
            req = load_request(path.stem, now)
        except RequestError:
            continue
        if req.status == "pending":
            out.append(req)
    return out


# --- the request flow (CLI `request-approval`, MCP portunus_request_approval) -

MAX_BAD_CODES = 3
_CODE_FILE_PLACEHOLDER = "<file path returned by the card>"


def _grant(broker, req: ApprovalRequest, approver: str, channel: str, factor: str) -> dict:
    broker.approve(req.name, ttl_seconds=WINDOW_SECONDS, approver=approver, once=True,
                   requester=req.requester, channel=channel, request_id=req.id,
                   factor=factor)
    decide(req, "approved", approver, channel)
    return {"approved": True, "request_id": req.id, "name": req.name,
            "channel": channel, "uses": 1, "window_seconds": WINDOW_SECONDS,
            "note": f"one resolve of {req.name} by {req.requester} within "
                    f"{WINDOW_SECONDS // 60} minutes"}


def _deny(broker, req: ApprovalRequest, approver: str, channel: str, why: str) -> None:
    ref = broker.registry.get(req.name)
    broker.audit.append("approve", ref.sm_name if ref else req.name,
                        f"denied:{why} requester:{req.requester} channel:{channel} "
                        f"request:{req.id}")
    decide(req, "denied", approver, channel)


def _bad_code(broker, req: ApprovalRequest, channel: str) -> dict:
    tries = int(req.extra.get("bad_codes", "0")) + 1
    req.extra["bad_codes"] = str(tries)
    if tries >= MAX_BAD_CODES:
        _deny(broker, req, "", channel, "bad-code")
        return {"error": "the code did not match three times; the request is denied",
                "request_id": req.id, "approved": False}
    _write(req)
    ref = broker.registry.get(req.name)
    broker.audit.append("approve", ref.sm_name if ref else req.name,
                        f"refused:bad-code requester:{req.requester} channel:{channel} "
                        f"request:{req.id}")
    return {"error": "the code did not match; nothing approved", "request_id": req.id,
            "approved": False, "tries_left": MAX_BAD_CODES - tries}


def human_instruction(req: ApprovalRequest) -> str:
    return f"portunus approve {req.name} --request {req.id}"


def _t3_handoff(req: ApprovalRequest, cli: bool) -> dict:
    from .prompt import T3_CARD_TOOL, T3_LABEL_MAX, T3_SURFACE, _one_line

    then: dict = {"tool": "portunus_request_approval",
                  "arguments": {"name": req.name, "request_id": req.id,
                                "code_file": _CODE_FILE_PLACEHOLDER}}
    if cli:
        then["cli"] = ["portunus", "request-approval", req.name, "--request", req.id,
                       "--code-file", _CODE_FILE_PLACEHOLDER]
    return {
        "approved": False, "pending": True, "surface": T3_SURFACE, "request_id": req.id,
        "name": req.name, "summary": req.summary(),
        "card": {"tool": T3_CARD_TOOL, "arguments": {
            "label": _one_line(req.summary(), T3_LABEL_MAX),
            "reason": "To allow this ONCE, type the 6-digit code from your authenticator "
                      "app. Dismiss the card to refuse.",
            "delivery": "file"}},
        "then": then,
        "note": "Call the card tool, then call portunus_request_approval again with "
                "code_file set to the path it returned. Do not read that file. If the "
                "human dismisses the card, the request stays pending and expires.",
    }


def _ask_by_dialog(broker, req: ApprovalRequest, surface: str, *, runner=None, which=None,
                   timeout: int = 300) -> dict:
    import shutil
    import subprocess

    from .prompt import PromptCancelled, _ask_dialog

    runner = runner or subprocess.run
    which = which or shutil.which
    channel = f"dialog:{surface}"
    text = (f"{req.summary()}.\n\nTo allow this ONCE, type the 6-digit code from your "
            "authenticator app. Cancel to refuse.")
    for _ in range(MAX_BAD_CODES):
        try:
            code = _ask_dialog(surface, text, "Portunus approval", runner, timeout, which)
        except PromptCancelled:
            _deny(broker, req, os.environ.get("USER", "unknown"), channel, "cancelled")
            return {"approved": False, "denied": True, "request_id": req.id,
                    "channel": channel}
        if verify_code(code):
            return _grant(broker, req, os.environ.get("USER", "unknown"), channel, "totp")
        text = (f"{req.summary()}.\n\nThat code did not match. Type the current 6-digit "
                "code from your authenticator app, or Cancel to refuse.")
    _deny(broker, req, "", channel, "bad-code")
    return {"error": "the code did not match three times; the request is denied",
            "request_id": req.id, "approved": False}


def complete_with_code_file(broker, req: ApprovalRequest, code_file: str) -> dict:
    """Step two of the t3 channel: the card saved what the human typed."""
    from .prompt import PromptError, read_value_file

    try:
        code = read_value_file(code_file).strip()
    except PromptError as exc:
        return {"error": str(exc), "request_id": req.id, "approved": False}
    try:
        os.unlink(code_file)
    except OSError:
        pass
    if verify_code(code):
        return _grant(broker, req, os.environ.get("USER", "unknown"), "t3", "totp")
    return _bad_code(broker, req, "t3")


def run_request(registry, broker, name: str, purpose: str = "", amount: str = "",
                merchant: str = "", surface: str = "auto", request_id: str = "",
                code_file: str = "", cli: bool = False, env=None, **dialog_kwargs) -> dict:
    """Ask the human to allow ONE use of a gated reference. Never returns a
    value. See the module docstring for the flow."""
    from .prompt import (DIALOG_SURFACES, T3_SURFACE, PromptError, available_surfaces,
                         choose_surface)

    ref = registry.get(name)
    if ref is None:
        return {"error": f"unknown reference: {name}"}
    if request_id:
        try:
            req = load_request(request_id)
        except RequestError as exc:
            return {"error": str(exc)}
        if req.name != name:
            return {"error": f"request {request_id} is for {req.name}, not {name}"}
        if req.status != "pending":
            return {"error": f"request {request_id} is {req.status}", "request_id": req.id,
                    "approved": req.status == "approved"}
        if not code_file:
            return {"error": "code_file is required to complete a request "
                             "(the path the T3 Code card returned)"}
        return complete_with_code_file(broker, req, code_file)
    if code_file:
        return {"error": "code_file completes a request; pass request_id too"}
    if ref.approval != "required":
        return {"approved": True, "needed": False, "name": name,
                "note": f"{name} is not gated; no approval is needed"}
    try:
        req = create_request(name, purpose, amount, merchant, env=env)
    except RequestError as exc:
        return {"error": str(exc)}
    broker.audit.append("approval_request", ref.sm_name,
                        f"requested:{req.id} requester:{req.requester}")
    pending = {"approved": False, "pending": True, "request_id": req.id, "name": name,
               "summary": req.summary(), "expires_in_seconds": REQUEST_TTL,
               "ask_human_to_run": human_instruction(req)}
    try:
        surface = choose_surface(surface, env)
    except PromptError as exc:
        return {**pending, "warning": str(exc)}
    if surface == "cli" or not factor_enrolled():
        if surface not in ("auto", "cli"):
            pending["warning"] = ("no approval factor is enrolled, so only the human's own "
                                  "terminal can approve: they run `portunus approval enroll` "
                                  "once to approve from T3 Code or a dialog")
        pending["surface"] = "cli"
        return pending
    if surface == T3_SURFACE:
        return _t3_handoff(req, cli)
    if surface == "auto":
        options = [s for s in available_surfaces(env=env) if s in DIALOG_SURFACES]
        if not options:
            pending["surface"] = "cli"
            return pending
        surface = options[0]
    if surface not in DIALOG_SURFACES:
        pending["surface"] = "cli"
        pending["warning"] = f"{surface} cannot take an approval; ask the human instead"
        return pending
    return _ask_by_dialog(broker, req, surface, **dialog_kwargs)
