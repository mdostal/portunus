"""Agent harness setup -- portunus agent init/status. Wires the MCP server and
usage skills into whatever AI coding agent CLIs are already on the machine.
Zero secret-boundary surface: nothing here touches a vault, registry, or
value -- verified structurally in test_cli_agent.py, not just by omission."""
import filecmp
import subprocess
from pathlib import Path

import pytest

from portunus import agent_setup


REPO_ROOT = Path(__file__).resolve().parent.parent


def test_detect_harnesses_reports_presence_via_which(monkeypatch):
    monkeypatch.setattr(
        agent_setup.shutil, "which",
        lambda name: "/usr/local/bin/claude" if name == "claude" else None,
    )
    assert agent_setup.detect_harnesses() == {"claude": True, "codex": False}


def test_mcp_registered_true_for_claude_via_targeted_get(monkeypatch):
    """Deliberately NOT `claude mcp list` -- that health-checks every
    registered server (real-world finding: 30+ seconds on a machine with
    several configured, one of which alone eats a 30s timeout). `mcp get
    <name>` is a fast, targeted lookup."""
    def fake_run(argv, **kwargs):
        assert argv == ["claude", "mcp", "get", "portunus"]
        return subprocess.CompletedProcess(argv, 0, stdout="portunus:\n  Status: ✔ Connected\n", stderr="")

    monkeypatch.setattr(agent_setup.subprocess, "run", fake_run)
    assert agent_setup.mcp_registered("claude") is True


def test_mcp_registered_true_for_codex_via_list(monkeypatch):
    """Codex's own `mcp list` is fast (no per-server health check), unlike
    Claude Code's -- safe to use directly."""
    def fake_run(argv, **kwargs):
        assert argv == ["codex", "mcp", "list"]
        return subprocess.CompletedProcess(argv, 0, stdout="Name     ...\nportunus ...\n", stderr="")

    monkeypatch.setattr(agent_setup.subprocess, "run", fake_run)
    assert agent_setup.mcp_registered("codex") is True


def test_mcp_registered_false_on_nonzero_exit(monkeypatch):
    monkeypatch.setattr(
        agent_setup.subprocess, "run",
        lambda argv, **kwargs: subprocess.CompletedProcess(argv, 1, stdout="", stderr="boom"),
    )
    assert agent_setup.mcp_registered("claude") is False


def test_mcp_registered_false_when_binary_missing(monkeypatch):
    def fake_run(argv, **kwargs):
        raise FileNotFoundError("no such file")

    monkeypatch.setattr(agent_setup.subprocess, "run", fake_run)
    assert agent_setup.mcp_registered("codex") is False


def test_mcp_registered_false_for_unknown_harness(monkeypatch):
    assert agent_setup.mcp_registered("cursor") is False


def test_register_mcp_skips_when_already_registered(monkeypatch):
    monkeypatch.setattr(agent_setup, "mcp_registered", lambda harness: True)
    calls = []
    monkeypatch.setattr(agent_setup, "_run", lambda argv, **kwargs: calls.append(argv))
    assert agent_setup.register_mcp("claude") is True
    assert calls == []  # never shelled out again -- already registered is a no-op success


def test_register_mcp_builds_correct_argv_for_claude(monkeypatch):
    monkeypatch.setattr(agent_setup, "mcp_registered", lambda harness: False)
    seen = {}

    def fake_run(argv, **kwargs):
        seen["argv"] = argv
        return subprocess.CompletedProcess(argv, 0, stdout="", stderr="")

    monkeypatch.setattr(agent_setup, "_run", fake_run)
    assert agent_setup.register_mcp("claude") is True
    assert seen["argv"] == ["claude", "mcp", "add", "--scope", "user", "portunus", "--", "portunus", "mcp"]


def test_register_mcp_builds_correct_argv_for_codex(monkeypatch):
    monkeypatch.setattr(agent_setup, "mcp_registered", lambda harness: False)
    seen = {}

    def fake_run(argv, **kwargs):
        seen["argv"] = argv
        return subprocess.CompletedProcess(argv, 0, stdout="", stderr="")

    monkeypatch.setattr(agent_setup, "_run", fake_run)
    assert agent_setup.register_mcp("codex") is True
    assert seen["argv"] == ["codex", "mcp", "add", "portunus", "--", "portunus", "mcp"]


def test_register_mcp_unknown_harness_returns_false(monkeypatch):
    monkeypatch.setattr(agent_setup, "mcp_registered", lambda harness: False)
    assert agent_setup.register_mcp("cursor") is False


def test_register_mcp_reports_false_on_failed_registration(monkeypatch):
    monkeypatch.setattr(agent_setup, "mcp_registered", lambda harness: False)
    monkeypatch.setattr(
        agent_setup, "_run",
        lambda argv, **kwargs: subprocess.CompletedProcess(argv, 1, stdout="", stderr="denied"),
    )
    assert agent_setup.register_mcp("claude") is False


def test_skill_names_matches_packaged_directory():
    names = agent_setup.skill_names()
    assert names  # never empty -- this repo always ships at least the four known skills
    assert set(names) == {p.name for p in agent_setup.AGENT_SKILLS_DIR.iterdir() if (p / "SKILL.md").is_file()}


def test_install_skills_copies_every_packaged_skill(tmp_path):
    written = agent_setup.install_skills(dest=tmp_path)
    assert set(written) == set(agent_setup.skill_names())
    for name in agent_setup.skill_names():
        installed = tmp_path / name / "SKILL.md"
        packaged = agent_setup.AGENT_SKILLS_DIR / name / "SKILL.md"
        assert installed.is_file()
        assert filecmp.cmp(installed, packaged, shallow=False)


def test_install_skills_is_idempotent(tmp_path):
    agent_setup.install_skills(dest=tmp_path)
    second = agent_setup.install_skills(dest=tmp_path)
    assert second == []  # nothing changed -- nothing reported as (re)written


def test_install_skills_repairs_a_modified_file(tmp_path):
    agent_setup.install_skills(dest=tmp_path)
    name = agent_setup.skill_names()[0]
    (tmp_path / name / "SKILL.md").write_text("tampered")
    written = agent_setup.install_skills(dest=tmp_path)
    assert written == [name]
    assert filecmp.cmp(tmp_path / name / "SKILL.md", agent_setup.AGENT_SKILLS_DIR / name / "SKILL.md", shallow=False)


def test_skills_installed_reports_presence_accurately(tmp_path):
    before = agent_setup.skills_installed(dest=tmp_path)
    assert all(v is False for v in before.values())
    agent_setup.install_skills(dest=tmp_path)
    after = agent_setup.skills_installed(dest=tmp_path)
    assert all(v is True for v in after.values())


def test_agent_status_shape(monkeypatch, tmp_path):
    monkeypatch.setattr(agent_setup, "detect_harnesses", lambda: {"claude": True, "codex": False})
    monkeypatch.setattr(agent_setup, "mcp_registered", lambda harness: harness == "claude")
    status = agent_setup.agent_status(dest=tmp_path)
    assert status["harnesses"] == {"claude": True, "codex": False}
    assert status["mcp_registered"] == {"claude": True, "codex": False}
    assert set(status["skills"].keys()) == set(agent_setup.skill_names())


def test_agent_init_defaults_to_every_detected_harness(monkeypatch, tmp_path):
    monkeypatch.setattr(agent_setup, "detect_harnesses", lambda: {"claude": True, "codex": False})
    calls = []
    monkeypatch.setattr(agent_setup, "register_mcp", lambda h: calls.append(h) or True)
    result = agent_setup.agent_init(dest=tmp_path)
    assert calls == ["claude"]  # codex not detected, never attempted
    assert result["skills_installed"] == agent_setup.skill_names()  # claude was a target -> skills installed


def test_agent_init_only_targets_explicitly_requested_harnesses(monkeypatch, tmp_path):
    monkeypatch.setattr(agent_setup, "detect_harnesses", lambda: {"claude": True, "codex": True})
    calls = []
    monkeypatch.setattr(agent_setup, "register_mcp", lambda h: calls.append(h) or True)
    result = agent_setup.agent_init(only=["codex"], dest=tmp_path)
    assert calls == ["codex"]  # claude present but NOT requested -- untouched
    assert result["skills_installed"] == []  # claude wasn't a target, so its skills aren't touched either


def test_agent_init_never_raises_when_one_harness_fails(monkeypatch, tmp_path):
    monkeypatch.setattr(agent_setup, "detect_harnesses", lambda: {"claude": True, "codex": True})
    monkeypatch.setattr(agent_setup, "register_mcp", lambda h: False if h == "codex" else True)
    result = agent_setup.agent_init(dest=tmp_path)
    assert result["mcp_registered"] == {"claude": True, "codex": False}


def test_packaged_skills_match_repo_dotclaude_copies():
    """Guards against silent drift between the two checked-in copies: the
    packaged one (src/portunus/agent_skills/, what ships/installs elsewhere)
    and this repo's own dev copy (.claude/skills/, what Claude Code loads
    when working in this repo). They must stay byte-identical."""
    for name in agent_setup.skill_names():
        packaged = agent_setup.AGENT_SKILLS_DIR / name / "SKILL.md"
        repo_copy = REPO_ROOT / ".claude" / "skills" / name / "SKILL.md"
        assert repo_copy.is_file(), f"{name} exists in agent_skills but not in .claude/skills"
        assert filecmp.cmp(packaged, repo_copy, shallow=False), f"{name} SKILL.md drifted between the two copies"
