"""Portunus with T3 Code -- a 1Password-CLI-compatible reader over the vault.

T3 Code (a GUI that runs coding agents) can answer a secret card or a
settings field with a *reference* to a secret provider instead of a typed
value. Stock T3 Code has one provider kind, 1Password, which it implements
by spawning the binary ``op`` found on PATH. This module is a small
stand-in for that binary, so a T3 Code "1Password" provider whose account
is ``portunus`` reads from the Portunus vault.

T3 Code makes exactly two calls, and these are the only ones accepted::

    op --cache=false read --account=portunus --no-newline op://portunus/<name>/value
    op --cache=false inject --account=portunus -i <template file>

``read`` prints the value with no trailing newline. ``inject`` prints the
template with every ``{{ op://portunus/<name>/value }}`` token replaced, or
nothing at all if any one of them cannot be read.

This is the ONLY code path in Portunus that writes a value to stdout. The
rest of the codebase hands a value to a boundary sink (an exec'd argv, a
0600 file, a callable) and never prints it. So the reader is fenced in:

  1. Off by default. Nothing is read until a human runs
     ``portunus t3 enable`` (a 0600 flag file in the Portunus home).
  2. Never to a terminal. If stdout is a TTY the reader refuses, so the
     value cannot land in a scrollback or a terminal recording.
  3. Only for T3 Code. The direct parent process must look like the T3
     Code server -- executable ``t3code``, ``t3``, ``node`` or ``electron``,
     or a name listed in ``PORTUNUS_T3_PARENTS`` -- and must not be a shell
     or an agent CLI (``sh``, ``bash``, ``zsh``, ``fish``, ``dash``,
     ``claude``, ``codex``, ``python*``). An agent that types ``op read ...``
     in its own shell is refused.
  4. Only the ``portunus`` account. ``--account`` must be exactly
     ``portunus``, so a T3 Code provider configured for a real 1Password
     account never reaches the vault through this launcher.
  5. Only ``op://portunus/<name>/value`` references, and only for names in
     the registry.
  6. Every read goes through ``Resolver`` and so through
     ``Broker.check_injectable``: lifecycle state, the approval gate and
     role policy apply exactly as for ``portunus resolve``. Nothing is
     cached between calls.
  7. Every refusal exits 1 with a one-line reason on stderr, nothing on
     stdout, and a ``t3_provider_read`` audit entry (``denied:<reason>``).
     Every value written is audited as ``t3_provider_read`` / ``ok`` on top
     of the resolver's own ``resolve`` entry.

Guard rail 3 raises the bar; it is not a security boundary. A process
running as the same user can arrange to have an allowed parent (for
example by starting ``node`` itself). The control that holds against that
is the approval gate: a gated reference (``portunus gate <name>``) is
unreadable here, as everywhere, until a human runs ``portunus approve``.
"""
from __future__ import annotations

import json
import os
import re
import shutil
import stat
import subprocess
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Callable, Dict, Iterable, List, Optional, Sequence, Tuple

from .atomicio import atomic_write
from .audit import AuditChain
from .backend import BackendError
from .broker import ApprovalRequired, NotAuthorized, NotInjectable
from .paths import home, home_path
from .resolver import UnknownReference

ACCOUNT = "portunus"
PROVIDER_NAME = "Portunus"
REFERENCE_FORMAT = "op://portunus/<reference-name>/value"
STATE_FILE = "t3.json"
SHIM_NAME = "op"
SHIM_MARKER = "# portunus-t3-op-shim"
PARENTS_ENV = "PORTUNUS_T3_PARENTS"
THREAD_ENV = "T3CODE_THREAD_ID"
AUDIT_ACTION = "t3_provider_read"

MAX_TEMPLATE_BYTES = 256 * 1024
MAX_REFERENCES = 64

# T3 Code's own reference grammar (packages/secret-provider-onepassword):
# a name is letters, digits, dot, underscore, dash, with spaces allowed
# inside but not at either end.
_NAME = r"[A-Za-z0-9._-](?:[A-Za-z0-9 ._-]*[A-Za-z0-9._-])?"
_NAME_RE = re.compile(rf"^{_NAME}$")
REFERENCE_RE = re.compile(rf"^op://portunus/({_NAME})/value$")
# Any {{ op://... }} token; each one must then pass REFERENCE_RE.
_TOKEN_RE = re.compile(r"\{\{ ?(op://[^{}]*?) ?\}\}")

DEFAULT_PARENTS = ("t3code", "t3", "node", "electron")
SHELLS = ("sh", "bash", "zsh", "fish", "dash")
AGENT_CLIS = ("claude", "codex")
# Parents that only tell us "some JavaScript program": T3 Code's server
# when it runs under plain node, but also an agent CLI written in
# JavaScript. For these the script path is checked too.
_GENERIC_RUNTIMES = ("node", "electron")
_AGENT_PATH_SEGMENTS = ("claude", "claude-code", "codex")


class BadReference(ValueError):
    """Not an ``op://portunus/<name>/value`` reference. The message never
    repeats the input: a mistyped call could have put a value there."""


class ShimError(RuntimeError):
    """The ``op`` launcher could not be installed or removed."""


class OpUsage(Exception):
    """The argv is not one of the two invocations T3 Code makes."""


# --- references -------------------------------------------------------------

def t3_reference(name: str) -> str:
    """The reference to type into T3 Code for the Portunus reference `name`.
    Raises BadReference when the name cannot be written in T3 Code's
    reference grammar (it has a slash, a colon, a leading space, ...)."""
    if not isinstance(name, str) or not _NAME_RE.match(name):
        raise BadReference(
            "this reference name cannot be used from T3 Code: T3 Code accepts letters, "
            "digits, '.', '_' and '-' (spaces only inside the name)"
        )
    return f"op://{ACCOUNT}/{name}/value"


def parse_reference(reference: str) -> str:
    """The Portunus reference name inside ``op://portunus/<name>/value``.
    Anything else -- another vault segment, another field, a query suffix
    -- raises BadReference."""
    match = REFERENCE_RE.match(reference) if isinstance(reference, str) else None
    if not match:
        raise BadReference(f"not a Portunus reference (want {REFERENCE_FORMAT})")
    return match.group(1)


# --- the opt-in flag --------------------------------------------------------

def state_path() -> Path:
    return home_path() / STATE_FILE


def is_enabled() -> bool:
    """True only after `portunus t3 enable`. Read-only: never creates the
    Portunus home. A missing or unreadable flag file means disabled."""
    try:
        return json.loads(state_path().read_text() or "{}").get("enabled") is True
    except (OSError, ValueError, AttributeError):
        return False


def set_enabled(on: bool) -> None:
    atomic_write(home() / STATE_FILE, json.dumps({"enabled": bool(on)}), mode=0o600)


# --- the `op` launcher ------------------------------------------------------

def portunus_command() -> List[str]:
    """How to start this Portunus from anywhere: the absolute path of the
    running `portunus` executable, else the one on PATH, else this
    interpreter with `-m portunus`."""
    arg0 = sys.argv[0] if sys.argv else ""
    if os.path.basename(arg0) == "portunus" and os.path.isfile(arg0) and os.access(arg0, os.X_OK):
        return [os.path.abspath(arg0)]
    found = shutil.which("portunus")
    if found:
        return [os.path.abspath(found)]
    return [sys.executable, "-m", "portunus"]


def default_shim_dir() -> Path:
    """Next to the `portunus` executable, which is on PATH wherever
    `portunus` itself is; `~/.local/bin` when that cannot be told."""
    command = portunus_command()
    if len(command) == 1:
        return Path(command[0]).parent
    return Path.home() / ".local" / "bin"


def _shell_quote(arg: str) -> str:
    return "'" + arg.replace("'", "'\"'\"'") + "'"


def shim_text(command: Sequence[str]) -> str:
    """The launcher. `exec` matters: it keeps T3 Code as the direct parent
    of the Portunus process, which is what guard rail 3 inspects."""
    quoted = " ".join(_shell_quote(part) for part in command)
    return (
        "#!/bin/sh\n"
        f"{SHIM_MARKER}\n"
        "# 1Password-CLI-compatible reader over the Portunus vault, for T3 Code.\n"
        "# Written by `portunus t3 install-shim`; remove it with `portunus t3 uninstall-shim`.\n"
        f'exec {quoted} t3 op "$@"\n'
    )


def is_our_shim(path: os.PathLike) -> bool:
    """True when `path` is a launcher this module wrote (it carries the
    marker line near the top)."""
    try:
        with open(path, "rb") as fh:
            head = fh.read(1024)
    except OSError:
        return False
    return SHIM_MARKER.encode() in head.splitlines()[:4]


def ops_on_path(path_env: Optional[str] = None) -> List[Path]:
    """Every executable named `op` on PATH, in PATH order."""
    raw = os.environ.get("PATH", "") if path_env is None else path_env
    found: List[Path] = []
    for directory in raw.split(os.pathsep):
        if not directory:
            continue
        candidate = Path(directory) / SHIM_NAME
        if candidate.is_file() and os.access(candidate, os.X_OK) and candidate not in found:
            found.append(candidate)
    return found


def _same_file(a: Path, b: Path) -> bool:
    try:
        return os.path.samefile(a, b)
    except OSError:
        return False


def install_shim(directory: Optional[os.PathLike] = None, force: bool = False,
                 path_env: Optional[str] = None,
                 command: Optional[Sequence[str]] = None) -> dict:
    """Write the `op` launcher into `directory`. Refuses (ShimError) to
    replace an `op` that is not ours, and refuses while another `op` -- in
    practice the real 1Password CLI -- is on PATH: whichever of the two came
    first would silently answer for the other. `force` overrides both."""
    target_dir = Path(directory).expanduser() if directory else default_shim_dir()
    target = target_dir / SHIM_NAME
    exists = os.path.lexists(target)
    if exists and not is_our_shim(target) and not force:
        raise ShimError(
            f"{target} already exists and is not the Portunus launcher (is the 1Password CLI "
            "installed?). Nothing was changed. Pick another --dir, or pass --force to replace it"
        )
    foreign = [p for p in ops_on_path(path_env)
               if not _same_file(p, target) and not is_our_shim(p)]
    if foreign and not force:
        raise ShimError(
            f"another `op` is on PATH at {foreign[0]} (probably the 1Password CLI). T3 Code "
            "runs whichever `op` comes first on PATH, so the two would conflict. Nothing was "
            "changed. Pass --force to install anyway"
        )
    command = list(command) if command else portunus_command()
    target_dir.mkdir(parents=True, exist_ok=True)
    atomic_write(target, shim_text(command), mode=0o755)
    raw_path = os.environ.get("PATH", "") if path_env is None else path_env
    on_path = any(
        d and _same_file(Path(d), target_dir) for d in raw_path.split(os.pathsep)
    )
    warnings: List[str] = []
    if not on_path:
        warnings.append(
            f"{target_dir} is not on PATH here; T3 Code only finds the launcher if that "
            "directory is on the PATH of the T3 Code server"
        )
    if foreign:
        warnings.append(f"another `op` is also on PATH: {foreign[0]}")
    return {"path": str(target), "command": command, "replaced": exists,
            "on_path": on_path, "warnings": warnings}


def uninstall_shim(directory: Optional[os.PathLike] = None,
                   path_env: Optional[str] = None) -> dict:
    """Remove the launcher, and only the launcher: a file named `op`
    without our marker is never deleted. With no `directory`, looks next to
    `portunus` and along PATH."""
    if directory:
        target = Path(directory).expanduser() / SHIM_NAME
        if os.path.lexists(target) and not is_our_shim(target):
            raise ShimError(f"{target} is not the Portunus launcher; not removing it")
        candidates = [target]
    else:
        candidates = [default_shim_dir() / SHIM_NAME] + ops_on_path(path_env)
    removed: List[str] = []
    for candidate in candidates:
        if str(candidate) in removed or not is_our_shim(candidate):
            continue
        candidate.unlink()
        removed.append(str(candidate))
    return {"removed": removed}


# --- status -----------------------------------------------------------------

def status(env: Optional[Dict[str, str]] = None) -> dict:
    """Read-only report for `portunus t3 status`. Mutates nothing and does
    not create the Portunus home."""
    from . import prompt as prompt_mod

    env = dict(os.environ) if env is None else env
    found = ops_on_path(env.get("PATH", ""))
    first = found[0] if found else None
    ours = bool(first) and is_our_shim(first)
    try:
        surface = prompt_mod.choose_surface("auto", env)
    except prompt_mod.PromptError:
        surface = "invalid"
    enabled = is_enabled()
    return {
        "enabled": enabled,
        "launcher": {
            "installed": first is not None,
            "path": str(first) if first else None,
            "ours": ours,
        },
        "ready": enabled and ours,
        "in_t3_thread": bool(env.get(THREAD_ENV)),
        "prompt_surface": surface,
        "reference_format": REFERENCE_FORMAT,
        "provider": {"kind": "1Password", "name": PROVIDER_NAME, "account": ACCOUNT},
        "allowed_parents": sorted(allowed_parents(env)),
    }


# --- guard rail 3: who is asking --------------------------------------------

def _basename(name: str) -> str:
    """`/usr/bin/Node` -> `node`; also drops the ` (deleted)` Linux appends
    to a replaced binary, a login shell's leading dash and a `.exe`
    suffix (Claude Code's binary is `claude.exe` even on Linux)."""
    base = (name or "").strip()
    if base.endswith(" (deleted)"):
        base = base[: -len(" (deleted)")]
    base = re.split(r"[\\/]", base)[-1].lower().lstrip("-")
    if base.endswith(".exe"):
        base = base[:-4]
    return base


def _is_shell_or_agent(base: str) -> bool:
    return base in SHELLS or base in AGENT_CLIS or base.startswith("python")


def _is_agent_script(arg: str) -> bool:
    """A script path that belongs to an agent CLI, judged by its last three
    path segments (`.../@anthropic-ai/claude-code/cli.js`, `.../bin/codex`).
    Only the tail is looked at so a home directory named `claude` does not
    count."""
    segments = [s for s in re.split(r"[\\/]", arg.lower()) if s][-3:]
    if segments:
        segments[-1] = re.sub(r"\.(c|m)?js$|\.exe$", "", segments[-1])
    return any(segment in _AGENT_PATH_SEGMENTS for segment in segments)


def allowed_parents(env: Optional[Dict[str, str]] = None) -> set:
    env = os.environ if env is None else env
    extra = [_basename(part) for part in (env.get(PARENTS_ENV) or "").split(",")]
    return set(DEFAULT_PARENTS) | {name for name in extra if name}


def parent_allowed(exe_basename: str, argv: Sequence[str],
                   extra: Iterable[str] = ()) -> Tuple[bool, str]:
    """Does a parent process with this executable and argv look like the T3
    Code server? Pure. Returns (allowed, what was seen) -- the second item
    is a short label for the refusal message and the audit entry.

    Allowed: the executable is on the allow-list (plus `extra`), it is not
    a shell or an agent CLI, argv[0] does not name one either, and -- for a
    bare JavaScript runtime -- the script it runs is not an agent CLI's.
    `extra` cannot allow a shell or an agent CLI."""
    exe = _basename(exe_basename)
    if not exe:
        return False, "unknown"
    if _is_shell_or_agent(exe):
        return False, exe
    arg0 = _basename(argv[0]) if argv else ""
    if arg0 and _is_shell_or_agent(arg0):
        return False, arg0
    allowed = set(DEFAULT_PARENTS) | {_basename(name) for name in extra if name and name.strip()}
    if exe not in allowed:
        return False, exe
    if exe in _GENERIC_RUNTIMES:
        # node [options] script ...: look at the first two non-option
        # arguments (the script, or a --require'd module and the script).
        scripts = [arg for arg in list(argv)[1:] if arg and not arg.startswith("-")][:2]
        if any(_is_agent_script(arg) for arg in scripts):
            return False, "agent-cli"
    return True, exe


def inspect_parent(pid: Optional[int] = None) -> Tuple[str, List[str]]:
    """(executable basename, argv) of the parent process, or ("", []) when
    it cannot be read -- which the caller treats as a refusal. Linux reads
    /proc; elsewhere (macOS) asks `ps`."""
    pid = os.getppid() if pid is None else pid
    try:
        exe = os.readlink(f"/proc/{pid}/exe")
        with open(f"/proc/{pid}/cmdline", "rb") as fh:
            raw = fh.read()
        argv = [part.decode("utf-8", "replace") for part in raw.split(b"\0") if part]
        return os.path.basename(exe), argv
    except OSError:
        pass
    try:
        comm = subprocess.run(["ps", "-o", "comm=", "-p", str(pid)],
                              capture_output=True, text=True, timeout=5)
        args = subprocess.run(["ps", "-o", "args=", "-p", str(pid)],
                              capture_output=True, text=True, timeout=5)
    except (OSError, subprocess.TimeoutExpired):
        return "", []
    if comm.returncode != 0 or not comm.stdout.strip():
        return "", []
    return os.path.basename(comm.stdout.strip()), args.stdout.split()


# --- the `op` entry point ---------------------------------------------------

@dataclass
class OpCall:
    command: str               # "read" or "inject"
    account: Optional[str]
    reference: str = ""        # read
    template_path: str = ""    # inject


def parse_op_argv(argv: Sequence[str]) -> OpCall:
    """Accept the two invocations T3 Code makes and nothing else::

        [--cache=false] read --account=X --no-newline <reference>
        [--cache=false] inject --account=X -i <path>

    `--account X` and `--in-file <path>` spellings are tolerated. Raises
    OpUsage (with a message that never repeats an argument) otherwise."""
    command: Optional[str] = None
    account: Optional[str] = None
    no_newline = False
    template_path: Optional[str] = None
    positionals: List[str] = []
    args = list(argv)
    i = 0

    def value_after(flag: str) -> str:
        nonlocal i
        i += 1
        if i >= len(args):
            raise OpUsage(f"{flag} needs a value")
        return args[i]

    while i < len(args):
        arg = args[i]
        if arg == "--account" or arg.startswith("--account="):
            if account is not None:
                raise OpUsage("--account given twice")
            account = arg.split("=", 1)[1] if "=" in arg else value_after("--account")
        elif command is None:
            if arg == "--cache=false":
                pass
            elif arg in ("read", "inject"):
                command = arg
            else:
                raise OpUsage("only `read` and `inject` are supported")
        elif command == "read" and arg in ("--no-newline", "-n"):
            no_newline = True
        elif command == "inject" and (arg in ("-i", "--in-file") or arg.startswith("--in-file=")):
            if template_path is not None:
                raise OpUsage("-i given twice")
            template_path = arg.split("=", 1)[1] if "=" in arg else value_after(arg)
        elif command == "read" and not arg.startswith("-"):
            positionals.append(arg)
        else:
            raise OpUsage(f"unsupported argument for `{command}`")
        i += 1

    if command is None:
        raise OpUsage("only `read` and `inject` are supported")
    if command == "read":
        if len(positionals) != 1:
            raise OpUsage("`read` takes exactly one reference")
        if not no_newline:
            raise OpUsage("`read` is only supported with --no-newline")
        return OpCall("read", account, reference=positionals[0])
    if not template_path:
        raise OpUsage("`inject` needs -i <template file>")
    return OpCall("inject", account, template_path=template_path)


class _Refused(Exception):
    """One refusal: an audit reason slug, a stderr sentence, and the name to
    audit it under ("-" when there is no trustworthy name)."""

    def __init__(self, reason: str, message: str, secret: str = "-"):
        super().__init__(message)
        self.reason = reason
        self.message = message
        self.secret = secret


def _audit_refusal(secret: str, reason: str) -> None:
    """Best effort, and never the reason a refusal turns into a crash. A
    Portunus home that does not exist yet is not created just to record
    that something was refused."""
    try:
        if home_path().is_dir():
            AuditChain().append(AUDIT_ACTION, secret, f"denied:{reason}")
    except Exception:  # noqa: BLE001
        pass


def _read_template(path: str) -> str:
    try:
        st = os.stat(path)
        if not stat.S_ISREG(st.st_mode):
            raise _Refused("template", "the template is not a regular file")
        if st.st_size > MAX_TEMPLATE_BYTES:
            raise _Refused("template", "the template is larger than 256 KiB")
        with open(path, "rb") as fh:
            raw = fh.read(MAX_TEMPLATE_BYTES + 1)
    except OSError:
        raise _Refused("template", "the template file cannot be read")
    if len(raw) > MAX_TEMPLATE_BYTES:
        raise _Refused("template", "the template is larger than 256 KiB")
    try:
        return raw.decode("utf-8")
    except UnicodeDecodeError:
        raise _Refused("template", "the template is not UTF-8 text")


def _template_names(text: str) -> List[str]:
    """The reference name behind every token, in order. Refuses a template
    with no token, too many, a token that is not ours, or stray braces
    outside the tokens (nothing T3 Code writes has those)."""
    tokens = _TOKEN_RE.findall(text)
    if not tokens:
        raise _Refused("template", "the template has no op://portunus/<name>/value reference")
    if len(tokens) > MAX_REFERENCES:
        raise _Refused("template", f"the template has more than {MAX_REFERENCES} references")
    rest = _TOKEN_RE.sub("", text)
    if "{{" in rest or "}}" in rest:
        raise _Refused("template", "the template has something other than "
                                   "{{ op://portunus/<name>/value }} references in braces")
    names: List[str] = []
    for token in tokens:
        try:
            names.append(parse_reference(token))
        except BadReference:
            raise _Refused("reference", f"a reference in the template is not {REFERENCE_FORMAT}")
    return names


def _fetch(resolver, registry, name: str, values: Dict[str, str]) -> None:
    """Resolve one reference into `values` through the resolver's boundary
    sink, so lifecycle state, the approval gate and role policy are checked
    the same way as for every other resolve."""
    ref = registry.get(name)
    if ref is None:
        raise _Refused("unknown-reference", "no such reference in the Portunus registry", name)
    secret = ref.sm_name or name

    def sink(resolved: str) -> None:
        values[name] = resolved

    try:
        resolver.resolve_call(f"{{{{secret:{name}}}}}", sink)
    except UnknownReference:
        raise _Refused("unknown-reference", "no such reference in the Portunus registry", name)
    except ApprovalRequired:
        raise _Refused("approval-required",
                       f"the reference is gated; a human must run: portunus approve {name}", secret)
    except NotInjectable:
        raise _Refused("not-injectable",
                       f"the reference is not enabled; see: portunus status {name}", secret)
    except NotAuthorized:
        raise _Refused("not-authorized", "role policy does not allow this read", secret)
    except BackendError:
        raise _Refused("backend-error", "the vault backend could not return the value", secret)
    except Exception as exc:  # noqa: BLE001 -- fail closed, and keep stdout empty
        raise _Refused("error", f"the read failed ({type(exc).__name__})", secret)


def op_main(argv: Sequence[str], build: Callable[[], tuple], *, stdout=None, stderr=None,
            inspect: Optional[Callable[[], Tuple[str, List[str]]]] = None,
            env: Optional[Dict[str, str]] = None) -> int:
    """`portunus t3 op ...`, which the `op` launcher execs. Returns the exit
    code: 0 with the value (or the filled template) on stdout, 1 for a
    refusal, 2 for an invocation T3 Code does not make. Stdout stays empty
    on every path but success.

    `build` returns (registry, audit, broker, resolver) -- `cli._build`.
    `inspect` returns the parent's (executable basename, argv); `stdout`
    needs `isatty()` and either `.buffer.write` or a bytes `write`. Both are
    injectable for tests."""
    out = sys.stdout if stdout is None else stdout
    err = sys.stderr if stderr is None else stderr
    env = os.environ if env is None else env
    inspect = inspect or inspect_parent

    def fail(code: int, message: str) -> int:
        print(f"portunus t3 op: {message}", file=err)
        return code

    try:
        call = parse_op_argv(argv)
    except OpUsage as exc:
        _audit_refusal("-", "usage")
        return fail(2, str(exc))

    values: Dict[str, str] = {}
    try:
        if not is_enabled():
            raise _Refused("disabled", "the T3 Code reader is off; a human can turn it on "
                                       "with: portunus t3 enable")
        try:
            tty = bool(out.isatty())
        except Exception:  # noqa: BLE001
            tty = True
        if tty:
            raise _Refused("tty", "refusing to write a secret to a terminal")
        exe, parent_argv = inspect()
        allowed, seen = parent_allowed(exe, parent_argv, allowed_parents(env))
        if not allowed:
            label = re.sub(r"[^A-Za-z0-9._-]", "", seen)[:32] or "unknown"
            raise _Refused(f"parent:{label}",
                           f"only the T3 Code server may read through this launcher "
                           f"(the calling process is {label})")
        if call.account != ACCOUNT:
            raise _Refused("account", "this launcher only serves --account=portunus")

        if call.command == "read":
            try:
                names = [parse_reference(call.reference)]
            except BadReference:
                raise _Refused("reference", f"the reference is not {REFERENCE_FORMAT}")
            template = ""
        else:
            template = _read_template(call.template_path)
            names = _template_names(template)

        registry, _audit, broker, resolver = build()
        unique = list(dict.fromkeys(names))
        for name in unique:
            _fetch(resolver, registry, name, values)
        if call.command == "read":
            payload = values[names[0]].encode("utf-8")
        else:
            payload = _TOKEN_RE.sub(
                lambda match: values[parse_reference(match.group(1))], template,
            ).encode("utf-8")
        # Audit first: if the audit log cannot take the entry, nothing is
        # written.
        for name in unique:
            ref = registry.get(name)
            broker.audit.append(AUDIT_ACTION, (ref.sm_name if ref else "") or name, "ok")
        sink = getattr(out, "buffer", out)
        sink.write(payload)
        sink.flush()
        del payload
        return 0
    except _Refused as refusal:
        _audit_refusal(refusal.secret, refusal.reason)
        return fail(1, refusal.message)
    except Exception as exc:  # noqa: BLE001 -- never a traceback, never a partial value
        _audit_refusal("-", "error")
        return fail(1, f"the read failed ({type(exc).__name__})")
    finally:
        values.clear()
