"""source classification on findings -- log/local/git-history, public/
private via gh (portunus-leak-scan-git-awareness Story 02)."""
import subprocess
from unittest.mock import patch

from portunus.leakscan import (
    _resolve_repo_visibility,
    add_scan_path,
    add_scan_repo,
    run_scan,
)


def _init_repo(tmp_path, name="repo", remote=None):
    repo = tmp_path / name
    repo.mkdir()
    subprocess.run(["git", "init", "-q"], cwd=repo, check=True)
    subprocess.run(["git", "config", "user.email", "test@example.com"], cwd=repo, check=True)
    subprocess.run(["git", "config", "user.name", "Test"], cwd=repo, check=True)
    if remote:
        subprocess.run(["git", "remote", "add", "origin", remote], cwd=repo, check=True)
    return repo


def _commit(repo, filename, content, message):
    (repo / filename).write_text(content)
    subprocess.run(["git", "add", filename], cwd=repo, check=True)
    subprocess.run(["git", "commit", "-q", "-m", message], cwd=repo, check=True)


# ---------------------------------------------------------------------------
# source_kind for plain path-glob findings
# ---------------------------------------------------------------------------


def test_log_like_filename_is_classified_as_log(stack, tmp_path):
    f = tmp_path / "transcript.jsonl"
    f.write_text("uh oh: SECRET-VALUE-abc123-xyz\n")
    stack["registry"].add("x", "sm-x")
    stack["backend"].set("sm-x", "SECRET-VALUE-abc123-xyz")
    add_scan_path(str(f))

    result = run_scan(stack["registry"], stack["broker"], stack["backend"])
    assert result.findings[0].source_kind == "log"


def test_non_log_filename_is_classified_as_local(stack, tmp_path):
    f = tmp_path / "config.txt"
    f.write_text("uh oh: SECRET-VALUE-abc123-xyz\n")
    stack["registry"].add("x", "sm-x")
    stack["backend"].set("sm-x", "SECRET-VALUE-abc123-xyz")
    add_scan_path(str(f))

    result = run_scan(stack["registry"], stack["broker"], stack["backend"])
    assert result.findings[0].source_kind == "local"


def test_shell_history_filename_is_classified_as_log(stack, tmp_path):
    f = tmp_path / ".zsh_history"
    f.write_text("uh oh: SECRET-VALUE-abc123-xyz\n")
    stack["registry"].add("x", "sm-x")
    stack["backend"].set("sm-x", "SECRET-VALUE-abc123-xyz")
    add_scan_path(str(f))

    result = run_scan(stack["registry"], stack["broker"], stack["backend"])
    assert result.findings[0].source_kind == "log"


# ---------------------------------------------------------------------------
# source_kind + repo_visibility for git-history findings
# ---------------------------------------------------------------------------


def test_repo_finding_is_classified_as_git_history(stack, tmp_path):
    repo = _init_repo(tmp_path)
    stack["registry"].add("x", "sm-x")
    stack["backend"].set("sm-x", "SECRET-VALUE-abc123-xyz")
    _commit(repo, "config.txt", "leaked: SECRET-VALUE-abc123-xyz\n", "oops")
    add_scan_repo(str(repo))

    result = run_scan(stack["registry"], stack["broker"], stack["backend"])
    assert result.findings[0].source_kind == "git-history"
    assert result.findings[0].repo_path == str(repo)


def test_repo_with_no_remote_has_unknown_visibility(stack, tmp_path):
    repo = _init_repo(tmp_path)  # no remote configured
    stack["registry"].add("x", "sm-x")
    stack["backend"].set("sm-x", "SECRET-VALUE-abc123-xyz")
    _commit(repo, "config.txt", "leaked: SECRET-VALUE-abc123-xyz\n", "oops")
    add_scan_repo(str(repo))

    result = run_scan(stack["registry"], stack["broker"], stack["backend"])
    assert result.findings[0].repo_visibility == "unknown"


def test_resolve_repo_visibility_maps_gh_output_correctly(tmp_path):
    repo = _init_repo(tmp_path, remote="https://github.com/example/fake-repo")

    with patch("subprocess.run") as mock_run:
        def fake_run(cmd, **kwargs):
            class Result:
                returncode = 0
                stdout = ""
            r = Result()
            if cmd[:2] == ["git", "-C"] and "remote" in cmd:
                r.stdout = "https://github.com/example/fake-repo\n"
            elif cmd[0] == "gh":
                r.stdout = "PUBLIC\n"
            return r

        mock_run.side_effect = fake_run
        assert _resolve_repo_visibility(str(repo)) == "public"


def test_resolve_repo_visibility_unknown_when_gh_fails(tmp_path):
    repo = _init_repo(tmp_path, remote="https://github.com/example/fake-repo")

    with patch("subprocess.run") as mock_run:
        def fake_run(cmd, **kwargs):
            class Result:
                returncode = 0
                stdout = ""
            r = Result()
            if cmd[:2] == ["git", "-C"] and "remote" in cmd:
                r.stdout = "https://github.com/example/fake-repo\n"
            elif cmd[0] == "gh":
                r.returncode = 1
                r.stdout = ""
            return r

        mock_run.side_effect = fake_run
        assert _resolve_repo_visibility(str(repo)) == "unknown"


def test_gh_repo_view_called_once_per_repo_not_per_finding(stack, tmp_path):
    repo = _init_repo(tmp_path, remote="https://github.com/example/fake-repo")
    stack["registry"].add("x", "sm-x")
    stack["backend"].set("sm-x", "SECRET-VALUE-abc123-xyz")
    stack["registry"].add("y", "sm-y")
    stack["backend"].set("y", "OTHER-SECRET-VALUE-99999")
    stack["backend"].set("sm-y", "OTHER-SECRET-VALUE-99999")
    _commit(
        repo, "config.txt",
        "leaked: SECRET-VALUE-abc123-xyz\nalso: OTHER-SECRET-VALUE-99999\n",
        "oops, two secrets",
    )
    add_scan_repo(str(repo))

    call_count = {"gh": 0}
    real_run = subprocess.run

    def counting_run(cmd, **kwargs):
        if cmd and cmd[0] == "gh":
            call_count["gh"] += 1
        return real_run(cmd, **kwargs)

    with patch("subprocess.run", side_effect=counting_run):
        result = run_scan(stack["registry"], stack["broker"], stack["backend"])

    assert len(result.findings) >= 2
    assert call_count["gh"] <= 1
