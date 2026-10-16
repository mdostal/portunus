"""Boundary-only injection adapters (story 03): EnvVarAdapter, FileAdapter.

The boundary-invariant tests are the load-bearing verification here -- they
assert the adapters never leak the value via a return value, an exception
message, or the file/env content shape, including on the FAILURE path (not
just the happy path -- see structured-outline.md Part 7 finding #2)."""
import json
import os
import stat

import pytest

from portunus.adapters import AdapterError, EnvVarAdapter, FileAdapter

SENTINEL = "s3kr3t-do-not-leak-0xCAFE"


# --- EnvVarAdapter ---------------------------------------------------------

def test_env_adapter_sets_the_process_env_var(monkeypatch):
    monkeypatch.delenv("PORTUNUS_TEST_VAR", raising=False)
    EnvVarAdapter().inject(SENTINEL, var_name="PORTUNUS_TEST_VAR")
    assert os.environ["PORTUNUS_TEST_VAR"] == SENTINEL


def test_env_adapter_inject_returns_nothing():
    result = EnvVarAdapter().inject(SENTINEL, var_name="PORTUNUS_TEST_VAR2")
    assert result is None


def test_env_adapter_failure_path_never_leaks_value():
    with pytest.raises(AdapterError) as exc_info:
        EnvVarAdapter().inject(SENTINEL, var_name="")
    assert SENTINEL not in str(exc_info.value)


# --- FileAdapter -------------------------------------------------------

def test_file_adapter_env_format(tmp_path):
    target = tmp_path / "out.env"
    FileAdapter().inject(SENTINEL, path=str(target), fmt="env", key="MY_SECRET")
    content = target.read_text()
    assert content == f"MY_SECRET={SENTINEL}\n"
    mode = stat.S_IMODE(os.stat(target).st_mode)
    assert mode == 0o600


def test_file_adapter_json_format(tmp_path):
    target = tmp_path / "out.json"
    FileAdapter().inject(SENTINEL, path=str(target), fmt="json", key="my_secret")
    data = json.loads(target.read_text())
    assert data == {"my_secret": SENTINEL}
    mode = stat.S_IMODE(os.stat(target).st_mode)
    assert mode == 0o600


def test_file_adapter_yaml_format(tmp_path):
    target = tmp_path / "out.yaml"
    FileAdapter().inject(SENTINEL, path=str(target), fmt="yaml", key="my_secret")
    content = target.read_text()
    assert SENTINEL in content
    assert "my_secret" in content
    mode = stat.S_IMODE(os.stat(target).st_mode)
    assert mode == 0o600


def test_file_adapter_creates_parent_dirs(tmp_path):
    target = tmp_path / "nested" / "dir" / "out.env"
    FileAdapter().inject(SENTINEL, path=str(target), fmt="env", key="K")
    assert target.exists()


def test_file_adapter_rejects_unsupported_format(tmp_path):
    target = tmp_path / "out.toml"
    with pytest.raises(AdapterError) as exc_info:
        FileAdapter().inject(SENTINEL, path=str(target), fmt="toml", key="K")
    assert SENTINEL not in str(exc_info.value)
    assert not target.exists()


def test_file_adapter_failure_path_never_leaks_value(tmp_path):
    target = tmp_path / "out.env"
    with pytest.raises(AdapterError) as exc_info:
        FileAdapter().inject(SENTINEL, path=str(target), fmt="env", key="")
    assert SENTINEL not in str(exc_info.value)
    assert not target.exists()


def test_file_adapter_inject_returns_nothing(tmp_path):
    target = tmp_path / "out.env"
    result = FileAdapter().inject(SENTINEL, path=str(target), fmt="env", key="K")
    assert result is None


def test_file_adapter_file_is_never_group_or_world_readable(tmp_path, monkeypatch):
    # PANT-1003: the file must be 0600 from creation, not only after a later
    # chmod. With chmod disabled and a permissive umask, the mode must still
    # be 0600.
    monkeypatch.setattr(os, "chmod", lambda *a, **k: None)
    old_umask = os.umask(0)
    try:
        target = tmp_path / "out.env"
        FileAdapter().inject(SENTINEL, path=str(target), fmt="env", key="K")
    finally:
        os.umask(old_umask)
    assert (target.stat().st_mode & 0o777) == 0o600


def test_file_adapter_tightens_a_preexisting_file(tmp_path):
    target = tmp_path / "out.env"
    target.write_text("old")
    os.chmod(target, 0o644)
    FileAdapter().inject(SENTINEL, path=str(target), fmt="env", key="K")
    assert (target.stat().st_mode & 0o777) == 0o600
    assert target.read_text() == f"K={SENTINEL}\n"


# --- FileAdapter mode/group (PANT-1142) ---------------------------------

def test_file_adapter_group_readable_mode_and_group(tmp_path):
    target = tmp_path / "out.env"
    gid = os.getgid()
    FileAdapter().inject(SENTINEL, path=str(target), fmt="env", key="K", mode="0640", group=str(gid))
    st = os.stat(target)
    assert stat.S_IMODE(st.st_mode) == 0o640
    assert st.st_gid == gid
    assert target.read_text() == f"K={SENTINEL}\n"


def test_file_adapter_accepts_group_name(tmp_path):
    import grp
    name = grp.getgrgid(os.getgid()).gr_name
    target = tmp_path / "out.env"
    FileAdapter().inject(SENTINEL, path=str(target), fmt="env", key="K", mode="0660", group=name)
    assert stat.S_IMODE(os.stat(target).st_mode) == 0o660


@pytest.mark.parametrize("mode", ["0644", "0666", "0700", "0755", "abc"])
def test_file_adapter_rejects_world_or_exec_modes(tmp_path, mode):
    target = tmp_path / "out.env"
    with pytest.raises(AdapterError) as exc_info:
        FileAdapter().inject(SENTINEL, path=str(target), fmt="env", key="K", mode=mode)
    assert SENTINEL not in str(exc_info.value)
    assert not target.exists()


def test_file_adapter_unknown_group_fails_before_writing(tmp_path):
    target = tmp_path / "out.env"
    with pytest.raises(AdapterError):
        FileAdapter().inject(SENTINEL, path=str(target), fmt="env", key="K", group="no-such-group-xyz")
    assert not target.exists()


def test_file_adapter_group_it_cannot_set_removes_the_file_and_never_writes_value(tmp_path, monkeypatch):
    def deny(*a, **k):
        raise PermissionError(1, "Operation not permitted")
    monkeypatch.setattr(os, "fchown", deny)
    other_gid = os.getgid() + 12345
    target = tmp_path / "out.env"
    with pytest.raises(AdapterError) as exc_info:
        FileAdapter().inject(SENTINEL, path=str(target), fmt="env", key="K", mode="0640", group=str(other_gid))
    assert SENTINEL not in str(exc_info.value)
    assert not target.exists()


def test_file_adapter_skips_chown_when_file_already_has_the_group(tmp_path, monkeypatch):
    # A setgid shared directory already hands new files its group; the owner
    # can't chown to a group it isn't in, so the adapter mustn't try.
    def deny(*a, **k):
        raise PermissionError(1, "Operation not permitted")
    monkeypatch.setattr(os, "fchown", deny)
    target = tmp_path / "out.env"
    FileAdapter().inject(SENTINEL, path=str(target), fmt="env", key="K", mode="0640", group=str(os.getgid()))
    assert stat.S_IMODE(os.stat(target).st_mode) == 0o640


def test_describe_file_reports_owner_group_mode(tmp_path):
    from portunus.adapters import describe_file
    target = tmp_path / "out.env"
    FileAdapter().inject(SENTINEL, path=str(target), fmt="env", key="K")
    info = describe_file(str(target))
    assert info["mode"] == "0600"
    assert info["uid"] == os.getuid()
    assert "only" in info["readable_by"]
    assert SENTINEL not in json.dumps(info)
