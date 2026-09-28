"""Shared crash-safe whole-file write.

Every JSON state file in Portunus (registry, vault, bindings, views, roles,
leak-scan state, update cache) is rewritten whole via temp-file +
os.replace(). os.replace() alone is atomic against *concurrent readers*, but
not durable against a crash or power loss: without an fsync the rename can
reach disk before the temp file's data does, leaving an empty or truncated
registry/vault after reboot. atomic_write() is the one place that gets the
ordering right, so no caller hand-rolls it:

  1. write the new bytes to a unique temp file in the same directory
     (created 0600 by mkstemp, then set to `mode`),
  2. fsync the temp file -- its data is on disk BEFORE the rename,
  3. os.replace() it over the target,
  4. fsync the directory -- the rename itself is on disk AFTER os.replace().

The temp name is unique per call (mkstemp), not a fixed `<name>.tmp`, so two
unlocked writers can never os.replace() each other's half-written temp file
(the localvault crash test_localvault.py reproduces). Mutual exclusion for a
read-modify-write is still the caller's job -- see filelock.flock_path.
"""
from __future__ import annotations

import os
import tempfile
from pathlib import Path
from typing import Union


def _fsync_dir(directory: Path) -> None:
    fd = os.open(directory, os.O_RDONLY)
    try:
        os.fsync(fd)
    finally:
        os.close(fd)


def atomic_write(path: Path, data: Union[str, bytes], mode: int = 0o600) -> None:
    """Durably replace `path` with `data` (str is UTF-8 encoded). On any
    failure the temp file is removed and `path` is left untouched."""
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    if isinstance(data, str):
        data = data.encode("utf-8")
    fd, tmp = tempfile.mkstemp(dir=path.parent, prefix=f".{path.name}.", suffix=".tmp")
    try:
        with os.fdopen(fd, "wb") as fh:
            os.fchmod(fh.fileno(), mode)
            fh.write(data)
            fh.flush()
            os.fsync(fh.fileno())
        os.replace(tmp, path)
    except BaseException:
        try:
            os.unlink(tmp)
        except FileNotFoundError:
            pass
        raise
    _fsync_dir(path.parent)
