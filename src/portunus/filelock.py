"""Shared fcntl.flock primitive.

The exact idiom Registry._locked(), AuditChain._locked(), and
LocalEncryptedBackend._locked() each hand-roll per-file (exclusive,
non-blocking flock, polled up to a bounded timeout). Factored out here so a
caller that must coordinate a lock across more than one of those files at
once (the vault-backup coordinated snapshot, portunus-vault-backup story 02)
has one tested, generic building block instead of a fifth hand-rolled copy.
This module does not replace the three existing `_locked()` methods -- each
of those also does class-specific work (reload-before, flush-after) that a
bare lock primitive shouldn't own.
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
