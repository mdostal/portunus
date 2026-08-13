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
