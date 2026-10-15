"""Shared fcntl.flock primitive.

Exclusive, non-blocking flock polled up to a bounded timeout -- the one
exclusive-lock acquisition loop in the codebase. Registry._locked(),
AuditChain._locked() and LocalEncryptedBackend._locked() all build on it
(each adds its own class-specific work, e.g. Registry's reload-before /
flush-after), as do the per-file config stores (views, roles, bindings,
leak-scan state) and the multi-lock coordinated snapshot in backup.py.
"""
from __future__ import annotations

import fcntl
import time
from contextlib import contextmanager
from pathlib import Path

_LOCK_POLL_INTERVAL = 0.05
_LOCK_TIMEOUT = 10.0


class LockTimeout(TimeoutError):
    """Could not acquire the file lock within the timeout."""


@contextmanager
def flock_path(path: Path, timeout: float = _LOCK_TIMEOUT, poll_interval: float = _LOCK_POLL_INTERVAL):
    """Exclusive flock on `path` (the lock file itself, created if absent),
    held for the duration of the `with` block. Bare primitive: no reload,
    no flush -- just mutual exclusion, so callers with different read/write
    needs (a single component's own mutation, or a multi-file coordinated
    read) can build on the same tested acquisition/timeout/release logic.
    """
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    fh = open(path, "w")
    deadline = time.monotonic() + timeout
    acquired = False
    try:
        while time.monotonic() < deadline:
            try:
                fcntl.flock(fh, fcntl.LOCK_EX | fcntl.LOCK_NB)
                acquired = True
                break
            except OSError:
                time.sleep(poll_interval)
        if not acquired:
            raise LockTimeout(f"could not acquire lock within {timeout}s ({path})")
        yield
    finally:
        if acquired:
            fcntl.flock(fh, fcntl.LOCK_UN)
        fh.close()
