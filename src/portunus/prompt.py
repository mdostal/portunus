"""Out-of-band human secret entry -- `portunus prompt` / `portunus_prompt`.

An agent can TRIGGER this but never reads what the human types. The value
goes from a human-facing input surface (a desktop password dialog, a fresh
terminal window running a masked prompt, or pinentry) straight into the
local vault inside this process. The only thing handed back to the caller
is metadata: ``{"name", "state", "stored": True}``, ``{"cancelled": True}``
or ``{"error": ...}`` -- never the value, its length, or a hash of it.

That's the difference from `portunus drop --stdin` / `portunus_drop`, where
the value has to pass through the agent's own context first, and from
interactive `portunus drop`, which needs a real TTY that an agent's tool
channel doesn't have.

Surfaces, in auto-selection order:

  * macOS: ``osascript`` hidden-answer dialog, then Terminal.app
  * Linux (only with a DISPLAY / WAYLAND_DISPLAY): ``kdialog --password``,
    ``zenity --entry --hide-text``, a GUI ``pinentry``, then a terminal
    window (konsole / gnome-terminal / xterm)
  * ``ui`` (explicit only): registers a state=requested placeholder and
    opens the dashboard's ``?fulfill=<name>`` deep link. Asynchronous -- the
    human finishes in the browser, so this returns ``pending`` rather than
    ``stored``.

MCP form elicitation is deliberately not a surface: most clients route
elicitation responses through the model, which is exactly what this
command exists to avoid.
"""
from __future__ import annotations

import getpass
import json
import os
import select
import shutil
import subprocess
import sys
import tempfile
import time
from dataclasses import dataclass, field
from typing import Callable, Dict, List, Optional
from urllib.parse import unquote

DIALOG_SURFACES = ("osascript", "kdialog", "zenity", "pinentry")
TERMINAL_SURFACES = ("terminal-app", "konsole", "gnome-terminal", "xterm")
SURFACES = DIALOG_SURFACES + TERMINAL_SURFACES + ("ui",)

# GUI pinentry flavours only: a curses pinentry needs a TTY we don't have.
_PINENTRY_BINARIES = ("pinentry-qt", "pinentry-gnome3", "pinentry-gtk-2", "pinentry-mac")

DEFAULT_TIMEOUT = 300


class PromptError(RuntimeError):
    """The prompt could not be shown or its answer was unusable. The
    message never contains the value."""


class PromptCancelled(Exception):
    """The human dismissed the prompt (or it timed out)."""

    def __init__(self, reason: str = "cancelled"):
        super().__init__(reason)
        self.reason = reason


@dataclass
class PromptRequest:
    name: str
    sm_name: str
    scope: str = ""
    kind: str = "password"
    org: str = ""
    provider: str = ""
    project: str = ""
    env: str = ""
    tags: Dict[str, str] = field(default_factory=dict)
    description: str = ""
    purpose: str = ""
    group: str = ""
    repo: str = ""
    enable: bool = False
    gate: bool = False
    confirm: bool = False

    @property
    def title(self) -> str:
        return "Portunus: enter a secret"

    @property
    def text(self) -> str:
        where = "/".join(p for p in (self.org, self.project, self.env) if p)
        detail = f" ({self.provider})" if self.provider else ""
        lines = [f"An agent asked you to store '{self.name}'{detail} in Portunus."]
        if where:
            lines.append(f"Scope: {where}")
        if self.description:
            lines.append(self.description)
        lines.append("The agent will only learn whether it was stored, never the value.")
        return "\n".join(lines)


# --- surface detection ------------------------------------------------------

def _has_gui(env: Dict[str, str], platform: str) -> bool:
    if platform == "darwin":
        return True
    return bool(env.get("DISPLAY") or env.get("WAYLAND_DISPLAY"))


def _pinentry_binary(which: Callable[[str], Optional[str]]) -> Optional[str]:
    for binary in _PINENTRY_BINARIES:
        if which(binary):
            return binary
    return None


def available_surfaces(
    env: Optional[Dict[str, str]] = None,
    platform: Optional[str] = None,
    which: Optional[Callable[[str], Optional[str]]] = None,
) -> List[str]:
    """Synchronous surfaces usable right now, in preference order. `ui` is
    never listed: it's asynchronous and only used when asked for by name."""
    which = which or shutil.which
    env = dict(os.environ) if env is None else env
    platform = platform or sys.platform
    if not _has_gui(env, platform):
        return []
    found: List[str] = []
    if platform == "darwin":
        if which("osascript"):
            found += ["osascript", "terminal-app"]
        return found
    for binary in ("kdialog", "zenity"):
        if which(binary):
            found.append(binary)
    if _pinentry_binary(which):
        found.append("pinentry")
    for binary in ("konsole", "gnome-terminal", "xterm"):
        if which(binary):
            found.append(binary)
    return found


# --- dialog surfaces --------------------------------------------------------

def _strip_one_newline(raw: str) -> str:
    """Dialog tools terminate their answer with one newline. Remove exactly
    that and nothing else -- a password may legitimately start or end with
    whitespace."""
    if raw.endswith("\r\n"):
        return raw[:-2]
    if raw.endswith("\n"):
        return raw[:-1]
    return raw


def _applescript_quote(text: str) -> str:
    return '"' + text.replace("\\", "\\\\").replace('"', '\\"') + '"'


def _dialog_argv(surface: str, title: str, text: str) -> List[str]:
    if surface == "kdialog":
        return ["kdialog", "--title", title, "--password", text]
    if surface == "zenity":
        return ["zenity", "--entry", "--hide-text", "--title", title, "--text", text]
    if surface == "osascript":
        script = (
            f"display dialog {_applescript_quote(text)} default answer \"\" "
            f"with hidden answer with title {_applescript_quote(title)}"
        )
        return ["osascript", "-e", script, "-e", "text returned of result"]
    raise PromptError(f"not a dialog surface: {surface}")


def _pinentry_escape(text: str) -> str:
    return text.replace("%", "%25").replace("\n", "%0A").replace("\r", "%0D")


def _ask_pinentry(binary: str, title: str, text: str, prompt: str, runner, timeout: int) -> str:
    script = "".join(
        f"{line}\n" for line in (
            f"SETTITLE {_pinentry_escape(title)}",
            f"SETDESC {_pinentry_escape(text)}",
            f"SETPROMPT {_pinentry_escape(prompt)}",
            "GETPIN",
            "BYE",
        )
    )
    try:
        proc = runner([binary], input=script, capture_output=True, text=True, timeout=timeout)
    except subprocess.TimeoutExpired:
        raise PromptCancelled("timeout")
    except OSError as exc:
        raise PromptError(f"could not start {binary}: {exc.strerror}")
    value = None
    for line in proc.stdout.splitlines():
        if line.startswith("D "):
            value = unquote(line[2:])
        elif line.startswith("ERR"):
            raise PromptCancelled()
    if value is None:
        # GETPIN with an empty entry answers OK with no D line.
        return ""
    return value


def _ask_dialog(surface: str, text: str, title: str, runner, timeout: int, which) -> str:
    if surface == "pinentry":
        binary = _pinentry_binary(which)
        if not binary:
            raise PromptError("no GUI pinentry found")
        return _ask_pinentry(binary, title, text, "Secret:", runner, timeout)
    try:
        proc = runner(
            _dialog_argv(surface, title, text),
            capture_output=True, text=True, timeout=timeout,
        )
    except subprocess.TimeoutExpired:
        raise PromptCancelled("timeout")
    except OSError as exc:
        raise PromptError(f"could not start {surface}: {exc.strerror}")
    if proc.returncode != 0:
        # kdialog/zenity exit 1 on Cancel; osascript exits 1 with error -128.
        raise PromptCancelled()
    return _strip_one_newline(proc.stdout)


def read_from_dialog(surface: str, req: PromptRequest, runner=None,
                     timeout: int = DEFAULT_TIMEOUT, which=None) -> str:
    runner = runner or subprocess.run
    which = which or shutil.which
    value = _ask_dialog(surface, req.text, req.title, runner, timeout, which)
    if req.confirm:
        again = _ask_dialog(surface, "Enter the same value again to confirm.",
                            req.title, runner, timeout, which)
        if again != value:
            raise PromptError("values did not match, nothing stored")
    return value


# --- terminal surfaces ------------------------------------------------------

def _terminal_argv(surface: str, inner: List[str]) -> List[str]:
    # Every variant must BLOCK until the window closes, so the parent knows
    # when to give up waiting on the pipe.
    if surface == "konsole":
        return ["konsole", "--nofork", "-e", *inner]
    if surface == "gnome-terminal":
        return ["gnome-terminal", "--wait", "--", *inner]
    if surface == "xterm":
        return ["xterm", "-e", *inner]
    if surface == "terminal-app":
        command = " ".join(_shell_quote(a) for a in inner) + "; exit"
        script = (
            'tell application "Terminal"\n'
            "  activate\n"
            f"  set t to do script {_applescript_quote(command)}\n"
            "  repeat while busy of t\n    delay 0.5\n  end repeat\n"
            "end tell"
        )
        return ["osascript", "-e", script]
    raise PromptError(f"not a terminal surface: {surface}")


def _shell_quote(arg: str) -> str:
    return "'" + arg.replace("'", "'\"'\"'") + "'"


def tty_entry(fifo_path: str, banner: str) -> int:
    """Runs INSIDE the spawned terminal window. Masked prompt, twice (no
    visual feedback otherwise), then one JSON line down the FIFO to the
    waiting parent. Prints nothing about the value."""
    print(banner)
    print()
    message: dict
    try:
        value = getpass.getpass("value: ")
        confirm = getpass.getpass("value (confirm): ")
        if value != confirm:
            print("values did not match, nothing stored")
            message = {"error": "values did not match, nothing stored"}
        else:
            message = {"value": value}
    except (EOFError, KeyboardInterrupt):
        message = {"cancelled": True}
    with open(fifo_path, "w", encoding="utf-8") as fh:
        fh.write(json.dumps(message) + "\n")
    del message
    return 0


def read_from_terminal(surface: str, req: PromptRequest, popen=None,
                       timeout: int = DEFAULT_TIMEOUT) -> str:
    """The value travels child -> parent over a FIFO inside a private 0700
    directory: never argv, never env, never a regular file on disk."""
    popen = popen or subprocess.Popen
    workdir = tempfile.mkdtemp(prefix="portunus-prompt-")
    fifo = os.path.join(workdir, "answer")
    proc = None
    fd = None
    try:
        os.mkfifo(fifo, 0o600)
        inner = [sys.executable, "-m", "portunus", "_tty-entry", fifo, "--banner", req.text]
        try:
            proc = popen(_terminal_argv(surface, inner),
                         stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        except OSError as exc:
            raise PromptError(f"could not start {surface}: {exc.strerror}")
        fd = os.open(fifo, os.O_RDONLY | os.O_NONBLOCK)
        buf = b""
        deadline = time.monotonic() + timeout
        exited_at: Optional[float] = None
        while b"\n" not in buf:
            if time.monotonic() > deadline:
                raise PromptCancelled("timeout")
            ready, _, _ = select.select([fd], [], [], 0.2)
            chunk = os.read(fd, 65536) if ready else b""
            if chunk:
                buf += chunk
                continue
            # No writer yet looks the same as EOF on a FIFO; only treat it
            # as "closed without answering" once the window itself is gone
            # (with a short grace period for the last write to land).
            if proc.poll() is not None:
                exited_at = exited_at or time.monotonic()
                if time.monotonic() - exited_at > 1.0:
                    raise PromptCancelled()
            time.sleep(0.1)
        line = buf.split(b"\n", 1)[0]
        del buf
        try:
            message = json.loads(line.decode("utf-8"))
        except (ValueError, UnicodeDecodeError):
            raise PromptError("unreadable answer from the terminal prompt")
        if message.get("cancelled"):
            raise PromptCancelled()
        if "error" in message:
            raise PromptError(str(message["error"]))
        return str(message.get("value", ""))
    finally:
        if fd is not None:
            os.close(fd)
        if proc is not None and proc.poll() is None:
            proc.terminate()
        try:
            os.unlink(fifo)
        except OSError:
            pass
        try:
            os.rmdir(workdir)
        except OSError:
            pass


# --- orchestration ----------------------------------------------------------

def read_secret(req: PromptRequest, surface: str = "auto", *, runner=None,
                popen=None, which=None, env=None, platform=None,
                timeout: int = DEFAULT_TIMEOUT):
    """Return (surface_used, value). Raises PromptCancelled/PromptError."""
    if surface == "auto":
        options = available_surfaces(env=env, platform=platform, which=which)
        if not options:
            raise PromptError(
                "no human-facing input surface available (no desktop session, or none of "
                "kdialog/zenity/pinentry/osascript/a terminal emulator is installed) -- "
                "ask the human to run `portunus drop <name> <sm_name>` at their own terminal, "
                "or use --surface ui"
            )
        surface = options[0]
    if surface in DIALOG_SURFACES:
        return surface, read_from_dialog(surface, req, runner=runner, timeout=timeout, which=which)
    if surface in TERMINAL_SURFACES:
        return surface, read_from_terminal(surface, req, popen=popen, timeout=timeout)
    raise PromptError(f"unknown surface: {surface}")


def run_prompt(req: PromptRequest, registry, broker, backend, surface: str = "auto",
               open_ui: Optional[Callable[[str], dict]] = None, **kwargs) -> dict:
    """Prompt the human, store the answer, return metadata only.

    Every return path is a plain dict with no value in it:
    ``{"name", "state", "stored": True, "surface"}``, ``{"cancelled": True}``,
    ``{"name", "state": "requested", "stored": False, "pending": True, ...}``
    (ui surface) or ``{"error": ...}``.
    """
    if not hasattr(backend, "store"):
        return {
            "error": "prompt requires the local-encrypted backend "
            "(unset PORTUNUS_BACKEND or set it to unset/local)"
        }
    if surface == "ui":
        if open_ui is None:
            return {"error": "the ui surface is not available from here"}
        ref = registry.request(
            req.name, req.sm_name, org=req.org, provider=req.provider,
            project=req.project, env=req.env, tags=req.tags,
        )
        broker.audit.append("prompt", ref.sm_name or ref.name, "requested:ui")
        opened = open_ui(ref.name)
        if "error" in opened:
            return opened
        return {"name": ref.name, "state": ref.state, "stored": False, "pending": True,
                "surface": "ui", **opened}

    try:
        used, value = read_secret(req, surface, **kwargs)
    except PromptCancelled as exc:
        broker.audit.append("prompt", req.sm_name, f"cancelled:{exc.reason}")
        out = {"cancelled": True}
        if exc.reason != "cancelled":
            out["reason"] = exc.reason
        return out
    except PromptError as exc:
        return {"error": str(exc)}
    if not value:
        return {"error": "empty secret value; nothing stored"}

    ref = registry.add(
        req.name, req.sm_name, scope=req.scope, kind=req.kind, state="dropped",
        org=req.org, provider=req.provider, project=req.project, env=req.env,
        tags=req.tags, description=req.description, purpose=req.purpose,
        group=req.group, repo=req.repo,
    )
    backend.store(ref.sm_name, value)
    del value
    broker.audit.append("drop", ref.sm_name, f"stored:prompt:{used}")
    if req.gate:
        broker.gate(ref.name, on=True)
    if req.enable:
        ref = registry.set_state(ref.name, "enabled")
    ref = registry.require(ref.name)
    return {"name": ref.name, "state": ref.state, "stored": True,
            "gated": ref.approval == "required", "surface": used}
