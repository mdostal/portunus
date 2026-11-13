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
  * ``t3`` (explicit, or chosen by ``auto`` inside a T3 Code thread): the
    secret card T3 Code shows in the thread. Portunus cannot raise that card
    itself -- only the agent can call T3's ``request_secret`` tool -- so
    this surface is a two-step handoff. Step one returns a ``pending``
    handoff naming the card tool and its arguments and stores nothing. The
    card writes what the human typed to a private 0600 file and gives the
    agent the path, not the value. Step two (``value_file=<path>``) reads
    that file here, stores the value and deletes the file.

Inside a T3 Code thread (``T3CODE_THREAD_ID`` is set) ``auto`` picks ``t3``:
those threads are often driven from another machine, where a dialog on the
server's desktop is out of reach. ``PORTUNUS_PROMPT_SURFACE=<surface>``
overrides what ``auto`` picks; an explicit surface wins over both.

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
import stat
import subprocess
import sys
import tempfile
import time
from dataclasses import dataclass, field
from typing import Callable, Dict, List, Optional
from urllib.parse import unquote

DIALOG_SURFACES = ("osascript", "kdialog", "zenity", "pinentry")
TERMINAL_SURFACES = ("terminal-app", "konsole", "gnome-terminal", "xterm")
# Not a local dialog, so it stays out of the two tuples above and out of
# available_surfaces(): the T3 Code secret card, completed in two calls.
T3_SURFACE = "t3"
SURFACES = DIALOG_SURFACES + TERMINAL_SURFACES + ("ui", T3_SURFACE)

# T3 Code exports this to every agent process it starts; the Portunus MCP
# server and CLI inherit it from the agent.
T3_THREAD_ENV = "T3CODE_THREAD_ID"
# Overrides what surface="auto" picks. Any name in SURFACES.
SURFACE_ENV = "PORTUNUS_PROMPT_SURFACE"
T3_CARD_TOOL = "mcp__t3-code__request_secret"
T3_LABEL_MAX = 80
# Upper bound for a card's value file. A secret is a line of text or a key
# file, not a blob.
VALUE_FILE_MAX_BYTES = 64 * 1024
_VALUE_FILE_PLACEHOLDER = "<file path returned by the card>"

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


def choose_surface(surface: str = "auto", env: Optional[Dict[str, str]] = None) -> str:
    """Settle what ``auto`` means before any surface is opened. An explicit
    surface is returned untouched. Otherwise ``PORTUNUS_PROMPT_SURFACE``
    wins if set, then the T3 Code card inside a T3 Code thread, and failing
    both the answer stays ``auto`` (first available desktop surface)."""
    if surface != "auto":
        return surface
    env = os.environ if env is None else env
    override = (env.get(SURFACE_ENV) or "").strip()
    if override:
        if override not in SURFACES:
            raise PromptError(
                f"{SURFACE_ENV} is set to an unknown surface (want one of: {', '.join(SURFACES)})"
            )
        return override
    if env.get(T3_THREAD_ENV):
        return T3_SURFACE
    return "auto"


# --- t3 surface (T3 Code secret card) ---------------------------------------

def _one_line(text: str, limit: int) -> str:
    flat = " ".join(text.split())
    return flat if len(flat) <= limit else flat[: limit - 3].rstrip() + "..."


def _request_arguments(req: PromptRequest) -> dict:
    """The metadata the caller passed, as `portunus_prompt` arguments, so the
    completing call registers the reference the same way. Defaults are left
    out."""
    args: dict = {"name": req.name, "sm_name": req.sm_name,
                  "value_file": _VALUE_FILE_PLACEHOLDER}
    for key in ("provider", "project", "org", "env", "scope", "description",
                "purpose", "group", "repo"):
        if getattr(req, key):
            args[key] = getattr(req, key)
    if req.kind != "password":
        args["kind"] = req.kind
    if req.tags:
        args["tags"] = dict(req.tags)
    if req.enable:
        args["enable"] = True
    if req.gate:
        args["gate"] = True
    return args


def _request_cli(req: PromptRequest) -> List[str]:
    argv = ["portunus", "prompt", req.name, req.sm_name,
            "--value-file", _VALUE_FILE_PLACEHOLDER]
    for key in ("provider", "project", "org", "env", "scope", "description",
                "purpose", "group", "repo"):
        if getattr(req, key):
            argv += [f"--{key}", getattr(req, key)]
    if req.kind != "password":
        argv += ["--kind", req.kind]
    if req.tags:
        argv += ["--tags", ",".join(f"{k}={v}" for k, v in req.tags.items())]
    if req.enable:
        argv.append("--enable")
    if req.gate:
        argv.append("--gate")
    return argv


def t3_handoff(req: PromptRequest, cli: bool = False) -> dict:
    """Step one of the t3 surface: tell the agent which card to raise and
    how to finish. Pure -- touches neither the registry, the vault nor the
    audit log."""
    reason = (f"The value is stored in your Portunus vault as '{req.name}' "
              "and the agent never sees it.")
    if req.gate:
        reason += " Each use will need your approval."
    then: dict = {"tool": "portunus_prompt", "arguments": _request_arguments(req)}
    if cli:
        then["cli"] = _request_cli(req)
    return {
        "stored": False, "pending": True, "surface": T3_SURFACE, "name": req.name,
        "card": {
            "tool": T3_CARD_TOOL,
            "arguments": {
                "label": _one_line(req.description or req.name, T3_LABEL_MAX) or req.name,
                "reason": reason,
                "delivery": "file",
            },
        },
        "then": then,
        "note": "Call the card tool, then call portunus_prompt again with value_file set "
                "to the path it returned. Do not read, print or copy that file.",
    }


def _value_file_problem(st: os.stat_result) -> str:
    """Why a file with this stat result is not acceptable as a card's value
    file ("" when it is)."""
    if stat.S_ISLNK(st.st_mode):
        return "value_file is a symlink"
    if not stat.S_ISREG(st.st_mode):
        return "value_file is not a regular file"
    if st.st_uid != os.getuid():
        return "value_file is not owned by the current user"
    if st.st_mode & 0o077:
        return "value_file is readable by group or others (want mode 0600)"
    if st.st_size == 0:
        return "value_file is empty; nothing stored"
    if st.st_size > VALUE_FILE_MAX_BYTES:
        return f"value_file is larger than {VALUE_FILE_MAX_BYTES // 1024} KiB"
    return ""


def read_value_file(path: str) -> str:
    """Read the value a T3 Code card saved. Raises PromptError (never with
    the value in it) unless `path` is an absolute path to a private regular
    file of ours: not a symlink, owned by this uid, no group/other bits,
    between 1 byte and 64 KiB. Never deletes or modifies the file."""
    if not path or not os.path.isabs(path):
        raise PromptError("value_file must be an absolute path")
    try:
        before = os.lstat(path)
    except FileNotFoundError:
        raise PromptError("value_file does not exist")
    except OSError as exc:
        raise PromptError(f"cannot inspect value_file: {exc.strerror}")
    problem = _value_file_problem(before)
    if problem:
        raise PromptError(problem)
    try:
        fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK | os.O_CLOEXEC)
    except OSError as exc:
        raise PromptError(f"cannot open value_file: {exc.strerror}")
    try:
        # Re-check what was actually opened: the path could have been
        # swapped between lstat() and open().
        opened = os.fstat(fd)
        problem = _value_file_problem(opened)
        if not problem and (opened.st_dev, opened.st_ino) != (before.st_dev, before.st_ino):
            problem = "value_file changed while it was being opened"
        if problem:
            raise PromptError(problem)
        raw = b""
        while len(raw) <= VALUE_FILE_MAX_BYTES:
            chunk = os.read(fd, VALUE_FILE_MAX_BYTES + 1 - len(raw))
            if not chunk:
                break
            raw += chunk
    finally:
        os.close(fd)
    if len(raw) > VALUE_FILE_MAX_BYTES:
        raise PromptError(f"value_file is larger than {VALUE_FILE_MAX_BYTES // 1024} KiB")
    try:
        return raw.decode("utf-8")
    except UnicodeDecodeError:
        raise PromptError("value_file is not UTF-8 text")
    finally:
        del raw


def complete_from_file(req: PromptRequest, registry, broker, backend, value_file: str) -> dict:
    """Step two of the t3 surface: store what the card saved, then delete
    the card's file.

    The file is deleted only once the value is in the vault. Every earlier
    failure (an unsafe file, an empty value, a store that raises) leaves it
    where it is and says so, so the human does not have to type the value
    again."""
    try:
        value = _strip_one_newline(read_value_file(value_file))
    except PromptError as exc:
        return {"error": str(exc)}
    if not value:
        return {"error": "empty secret value; nothing stored"}

    existed = req.name in registry
    progress: List[str] = []
    try:
        result = _store_value(req, registry, broker, backend, value, T3_SURFACE,
                              progress=progress)
    except Exception as exc:  # noqa: BLE001 -- must decide the file's fate on any failure
        if not progress:
            # Nothing reached the vault. Undo a registry entry this call
            # created, and leave the file for a retry.
            if not existed and req.name in registry:
                try:
                    registry.remove(req.name)
                except Exception:  # noqa: BLE001
                    pass
            return {"error": f"could not store the value ({type(exc).__name__}); nothing stored. "
                             "The card's file was left in place: fix the problem and call "
                             "again with the same value_file, or delete the file"}
        # The value is in the vault but a later step (audit, gate, enable)
        # failed. The file has done its job, so it still goes.
        result = {"name": req.name, "stored": True, "surface": T3_SURFACE,
                  "warning": f"the value was stored but finishing up failed ({type(exc).__name__}); "
                             f"check `portunus status {req.name}`"}
    finally:
        del value

    try:
        os.unlink(value_file)
        result["file_removed"] = True
    except OSError:
        result["file_removed"] = False
        note = f"could not delete the card's file; delete it yourself without reading it: {value_file}"
        result["warning"] = f"{result['warning']}; {note}" if "warning" in result else note
    return result


def _store_value(req: PromptRequest, registry, broker, backend, value: str, used: str,
                 progress: Optional[List[str]] = None) -> dict:
    """Register the reference and store `value`: the one place a prompted
    value enters the vault, whichever surface it came from. Appends
    "stored" to `progress` the moment the vault has the value. Returns
    metadata only."""
    ref = registry.add(
        req.name, req.sm_name, scope=req.scope, kind=req.kind, state="dropped",
        org=req.org, provider=req.provider, project=req.project, env=req.env,
        tags=req.tags, description=req.description, purpose=req.purpose,
        group=req.group, repo=req.repo,
    )
    backend.store(ref.sm_name, value)
    del value
    if progress is not None:
        progress.append("stored")
    broker.audit.append("drop", ref.sm_name, f"stored:prompt:{used}")
    if req.gate:
        broker.gate(ref.name, on=True)
    if req.enable:
        ref = registry.set_state(ref.name, "enabled")
    ref = registry.require(ref.name)
    return {"name": ref.name, "state": ref.state, "stored": True,
            "gated": ref.approval == "required", "surface": used}


def run_prompt(req: PromptRequest, registry, broker, backend, surface: str = "auto",
               open_ui: Optional[Callable[[str], dict]] = None, value_file: str = "",
               cli: bool = False, env=None, **kwargs) -> dict:
    """Prompt the human, store the answer, return metadata only.

    Every return path is a plain dict with no value in it:
    ``{"name", "state", "stored": True, "surface"}``, ``{"cancelled": True}``,
    ``{"name", "state": "requested", "stored": False, "pending": True, ...}``
    (ui surface), ``{"stored": False, "pending": True, "surface": "t3",
    "card", "then", ...}`` (t3 surface, first call) or ``{"error": ...}``.

    `value_file` completes a t3 prompt: the path a T3 Code card returned.
    `cli` adds the equivalent `portunus prompt` argv to a t3 handoff.
    """
    if not hasattr(backend, "store"):
        return {
            "error": "prompt requires the local-encrypted backend "
            "(unset PORTUNUS_BACKEND or set it to unset/local)"
        }
    if value_file:
        if surface not in ("auto", T3_SURFACE):
            return {"error": f"value_file completes a t3 prompt and cannot be combined "
                             f"with surface={surface}"}
        return complete_from_file(req, registry, broker, backend, value_file)
    try:
        surface = choose_surface(surface, env)
    except PromptError as exc:
        return {"error": str(exc)}
    if surface == T3_SURFACE:
        return t3_handoff(req, cli=cli)
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
        used, value = read_secret(req, surface, env=env, **kwargs)
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
    return _store_value(req, registry, broker, backend, value, used)
