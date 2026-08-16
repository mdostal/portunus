"""git-repository history as a leak-scan target (portunus-leak-scan-git-
awareness Story 01). Always a full re-scan per repo per run -- git history
can be rewritten (rebase/force-push), which makes a byte-offset watermark
(correct for append-only log files) unsafe here."""
import subprocess

from portunus.leakscan import (
    add_scan_repo,
    load_scan_repos,
    remove_scan_repo,
    run_scan,
)


def _init_repo(tmp_path, name="repo"):
    repo = tmp_path / name
    repo.mkdir()
    subprocess.run(["git", "init", "-q"], cwd=repo, check=True)
    subprocess.run(["git", "config", "user.email", "test@example.com"], cwd=repo, check=True)
    subprocess.run(["git", "config", "user.name", "Test"], cwd=repo, check=True)
    return repo


def _commit(repo, filename, content, message):
    (repo / filename).write_text(content)
    subprocess.run(["git", "add", filename], cwd=repo, check=True)
    subprocess.run(["git", "commit", "-q", "-m", message], cwd=repo, check=True)


def test_add_remove_show_repos(home, tmp_path):
    repo = _init_repo(tmp_path)
    add_scan_repo(str(repo))
    assert load_scan_repos() == [str(repo)]
    remove_scan_repo(str(repo))
    assert load_scan_repos() == []


def test_add_scan_repo_is_idempotent(home, tmp_path):
    repo = _init_repo(tmp_path)
    add_scan_repo(str(repo))
    add_scan_repo(str(repo))
    assert load_scan_repos() == [str(repo)]


def test_scan_finds_a_value_in_git_history(stack, tmp_path):
    repo = _init_repo(tmp_path)
    stack["registry"].add("x", "sm-x")
    stack["backend"].set("sm-x", "SECRET-VALUE-abc123-xyz")
    _commit(repo, "config.txt", "leaked: SECRET-VALUE-abc123-xyz\n", "oops, a secret")
    add_scan_repo(str(repo))

    result = run_scan(stack["registry"], stack["broker"], stack["backend"])
    assert len(result.findings) == 1
    assert result.findings[0].ref_name == "x"


def test_scan_finds_a_value_even_after_a_later_commit_removes_it(stack, tmp_path):
    """The whole point of history scanning -- the working tree is clean,
    but the secret is still sitting in an old commit. (git log -p's diff
    format shows the removed line too, prefixed "-", so the value
    legitimately appears twice in the dump -- the original addition and
    the later removal's context -- hence >= 1, not an exact count.)"""
    repo = _init_repo(tmp_path)
    stack["registry"].add("x", "sm-x")
    stack["backend"].set("sm-x", "SECRET-VALUE-abc123-xyz")
    _commit(repo, "config.txt", "leaked: SECRET-VALUE-abc123-xyz\n", "oops, a secret")
    _commit(repo, "config.txt", "cleaned up\n", "remove the secret")
    add_scan_repo(str(repo))

    # working tree itself has no match
    assert "SECRET-VALUE-abc123-xyz" not in (repo / "config.txt").read_text()

    result = run_scan(stack["registry"], stack["broker"], stack["backend"])
    assert len(result.findings) >= 1
    assert all(f.ref_name == "x" for f in result.findings)


def test_scan_leaves_no_temp_dump_file_behind(stack, tmp_path):
    import glob as glob_module
    import tempfile

    repo = _init_repo(tmp_path)
    stack["registry"].add("x", "sm-x")
    stack["backend"].set("sm-x", "SECRET-VALUE-abc123-xyz")
    _commit(repo, "config.txt", "leaked: SECRET-VALUE-abc123-xyz\n", "oops")
    add_scan_repo(str(repo))

    before = set(glob_module.glob(f"{tempfile.gettempdir()}/portunus-leak-scan-repo-*"))
    run_scan(stack["registry"], stack["broker"], stack["backend"])
    after = set(glob_module.glob(f"{tempfile.gettempdir()}/portunus-leak-scan-repo-*"))
    assert after == before


def test_repo_scan_is_never_incremental(stack, tmp_path):
    """Full re-scan every run, deliberately -- proves the no-watermark-for
    -repos decision, not an accidental regression toward incremental
    behavior."""
    repo = _init_repo(tmp_path)
    stack["registry"].add("x", "sm-x")
    stack["backend"].set("sm-x", "SECRET-VALUE-abc123-xyz")
    _commit(repo, "config.txt", "leaked: SECRET-VALUE-abc123-xyz\n", "oops")
    add_scan_repo(str(repo))

    result1 = run_scan(stack["registry"], stack["broker"], stack["backend"])
    result2 = run_scan(stack["registry"], stack["broker"], stack["backend"])
    assert len(result1.findings) == 1
    assert len(result2.findings) == 1  # re-detected, not silently skipped


def test_no_repos_or_paths_configured_reports_that_explicitly(stack):
    result = run_scan(stack["registry"], stack["broker"], stack["backend"])
    assert result.configured_paths == []
    assert result.configured_repos == []
    assert result.findings == []


def test_repo_finding_path_is_stable_across_new_commits(stack, tmp_path):
    """New commits must not shift the stable finding key -- oldest-first
    (--reverse) dump ordering means existing content's position never
    moves as new commits are appended."""
    repo = _init_repo(tmp_path)
    stack["registry"].add("x", "sm-x")
    stack["backend"].set("sm-x", "SECRET-VALUE-abc123-xyz")
    _commit(repo, "config.txt", "leaked: SECRET-VALUE-abc123-xyz\n", "oops")
    add_scan_repo(str(repo))

    result1 = run_scan(stack["registry"], stack["broker"], stack["backend"])
    finding1 = result1.findings[0]

    _commit(repo, "other.txt", "unrelated new content\n", "a later, unrelated commit")

    result2 = run_scan(stack["registry"], stack["broker"], stack["backend"])
    finding2 = result2.findings[0]

    assert finding1.path == finding2.path
    assert finding1.line_number == finding2.line_number


def test_invalid_repo_path_is_a_harmless_skip_not_a_crash(stack, tmp_path):
    not_a_repo = tmp_path / "not-a-repo"
    not_a_repo.mkdir()
    stack["registry"].add("x", "sm-x")
    stack["backend"].set("sm-x", "SECRET-VALUE-abc123-xyz")
    add_scan_repo(str(not_a_repo))

    result = run_scan(stack["registry"], stack["broker"], stack["backend"])
    assert result.findings == []
