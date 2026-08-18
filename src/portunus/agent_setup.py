"""Agent harness setup -- ``portunus agent init``/``status``.

Wires Portunus into whatever AI coding agent CLIs are already on this machine
(Claude Code, Codex CLI today) so a fresh machine gets the same MCP tools and
usage skills this repo's own ``.claude/skills/`` already ships with, without a
human doing it by hand, one harness at a time. This is the single-command
onboarding path the desktop app and the docs site both point to.

Zero secret-boundary surface, by construction, not by discipline: every
operation here is local config plumbing (MCP server registration, copying
markdown skill files) -- nothing here imports ``Registry``/``Broker``/
``Resolver`` or touches a vault, a registry, or a secret value. See
tests/test_cli_agent.py for the structural check that keeps it that way.
"""
from __future__ import annotations

import filecmp
import shutil
import subprocess
from pathlib import Path
from typing import Dict, List, Optional

AGENT_SKILLS_DIR = Path(__file__).parent / "agent_skills"

# Ordered so agent_init()'s default target list (every present harness) has a
# stable, predictable order -- matters for tests and for readable output.
KNOWN_HARNESSES = ("claude", "codex")


def _run(argv: List[str], timeout: int = 15) -> Optional[subprocess.CompletedProcess]:
    try:
        return subprocess.run(argv, capture_output=True, text=True, timeout=timeout)
    except (OSError, subprocess.TimeoutExpired):
        return None


def detect_harnesses() -> Dict[str, bool]:
    """Which known agent CLIs are actually installed on this machine."""
    return {name: shutil.which(name) is not None for name in KNOWN_HARNESSES}


def mcp_registered(harness: str) -> bool:
    """Best-effort check -- targeted per-server lookup, never the harness's
    full ``mcp list`` (real-world finding: Claude Code's `mcp list` health-
    checks *every* registered server, including slow/unreachable ones --
    30+ seconds on a machine with several MCP servers configured, which
    made this check unreliable under a short timeout). False on any
    failure; never assumes registered when it can't tell."""
    if harness == "claude":
        result = _run(["claude", "mcp", "get", "portunus"])
    elif harness == "codex":
        result = _run(["codex", "mcp", "list"])
    else:
        return False
    if result is None or result.returncode != 0:
        return False
    return "portunus" in result.stdout


def register_mcp(harness: str) -> bool:
    """Registers ``portunus mcp`` with the given harness. Idempotent -- a
    no-op success if already registered, so re-running init is always safe."""
    if mcp_registered(harness):
        return True
    if harness == "claude":
        argv = ["claude", "mcp", "add", "--scope", "user", "portunus", "--", "portunus", "mcp"]
    elif harness == "codex":
        argv = ["codex", "mcp", "add", "portunus", "--", "portunus", "mcp"]
    else:
        return False
    result = _run(argv, timeout=30)
    return result is not None and result.returncode == 0


def skill_names() -> List[str]:
    """Names of every packaged usage skill (directory name == skill name)."""
    if not AGENT_SKILLS_DIR.is_dir():
        return []
    return sorted(p.name for p in AGENT_SKILLS_DIR.iterdir() if (p / "SKILL.md").is_file())


def _skills_dest(dest: Optional[Path]) -> Path:
    return dest if dest is not None else (Path.home() / ".claude" / "skills")


def skills_installed(dest: Optional[Path] = None) -> Dict[str, bool]:
    """Whether each packaged skill is present at dest (default
    ``~/.claude/skills``) -- presence only, doesn't check content freshness
    (install_skills() itself decides that)."""
    dest = _skills_dest(dest)
    return {name: (dest / name / "SKILL.md").is_file() for name in skill_names()}


def install_skills(dest: Optional[Path] = None) -> List[str]:
    """Copies every packaged skill into dest, creating it if needed.
    Overwrites only when the content actually differs, so re-running this is
    silent when nothing changed. Returns the names actually (re)written."""
    dest = _skills_dest(dest)
    dest.mkdir(parents=True, exist_ok=True)
    written = []
    for name in skill_names():
        src_file = AGENT_SKILLS_DIR / name / "SKILL.md"
        dest_dir = dest / name
        dest_file = dest_dir / "SKILL.md"
        if dest_file.is_file() and filecmp.cmp(src_file, dest_file, shallow=False):
            continue
        dest_dir.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(src_file, dest_file)
        written.append(name)
    return written


def agent_status(dest: Optional[Path] = None) -> Dict[str, object]:
    """Combined report: which harnesses are present, which have the MCP
    server registered, and which usage skills are installed."""
    harnesses = detect_harnesses()
    return {
        "harnesses": harnesses,
        "mcp_registered": {
            name: (mcp_registered(name) if present else False)
            for name, present in harnesses.items()
        },
        "skills": skills_installed(dest),
    }


def agent_init(only: Optional[List[str]] = None, dest: Optional[Path] = None) -> Dict[str, object]:
    """Idempotent, best-effort setup across every detected (or explicitly
    requested via ``only``) harness. One harness failing to register never
    blocks the others -- each is attempted and reported independently.

    Skills are Claude-Code-specific (Codex has no equivalent mechanism
    today), so they're only installed when ``"claude"`` is among the actual
    targets -- an explicit ``only=["codex"]`` leaves them untouched even if
    Claude Code also happens to be present on this machine.
    """
    harnesses = detect_harnesses()
    targets = only if only is not None else [name for name, present in harnesses.items() if present]
    mcp_results = {name: register_mcp(name) for name in targets}
    skills_written = install_skills(dest) if "claude" in targets else []
    return {
        "harnesses": harnesses,
        "requested": targets,
        "mcp_registered": mcp_results,
        "skills_installed": skills_written,
    }
