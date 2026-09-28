"""atomic_write(): the one crash-safe whole-file write every JSON state
file goes through (registry, vault, bindings, views, roles, leak-scan)."""
import os
import stat

import pytest

from portunus import atomicio
from portunus.atomicio import atomic_write


def test_fsyncs_temp_file_before_replace_and_directory_after(tmp_path, monkeypatch):
    """Crash-safety ordering: temp file data on disk BEFORE the rename,
    the rename itself on disk AFTER it. Without the first fsync a crash can
    leave an empty target; without the second the rename can be lost."""
    target = tmp_path / "registry.json"
    events = []
    real_fsync, real_replace = os.fsync, os.replace

    def fsync(fd):
        kind = "dir" if stat.S_ISDIR(os.fstat(fd).st_mode) else "file"
        events.append(f"fsync:{kind}")
        real_fsync(fd)

    def replace(src, dst):
        events.append("replace")
        real_replace(src, dst)

    monkeypatch.setattr(atomicio.os, "fsync", fsync)
    monkeypatch.setattr(atomicio.os, "replace", replace)

    atomic_write(target, '{"a": 1}')

    assert events == ["fsync:file", "replace", "fsync:dir"]
    assert target.read_text() == '{"a": 1}'


def test_writes_bytes_and_str_with_requested_mode(tmp_path):
    secret = tmp_path / "vault.enc.json"
    atomic_write(secret, b"\x00\x01")
    assert secret.read_bytes() == b"\x00\x01"
    assert secret.stat().st_mode & 0o777 == 0o600

    cache = tmp_path / "nested" / "update-check.json"
    atomic_write(cache, "{}", mode=0o644)
    assert cache.read_text() == "{}"
    assert cache.stat().st_mode & 0o777 == 0o644


def test_failed_write_leaves_target_intact_and_no_temp_file(tmp_path, monkeypatch):
    target = tmp_path / "registry.json"
    target.write_text("original")

    def boom(fd):
        raise OSError("disk full")

    monkeypatch.setattr(atomicio.os, "fsync", boom)
    with pytest.raises(OSError, match="disk full"):
        atomic_write(target, "replacement")

    assert target.read_text() == "original"
    assert sorted(p.name for p in tmp_path.iterdir()) == ["registry.json"]


def test_temp_name_is_unique_per_call(tmp_path, monkeypatch):
    """Not a fixed `<name>.tmp` -- two writers can never os.replace() each
    other's half-written temp file."""
    seen = []
    real_replace = os.replace
    monkeypatch.setattr(atomicio.os, "replace", lambda s, d: (seen.append(s), real_replace(s, d)))

    atomic_write(tmp_path / "x.json", "1")
    atomic_write(tmp_path / "x.json", "2")

    assert len(set(seen)) == 2
    assert all(os.path.dirname(s) == str(tmp_path) for s in seen)
