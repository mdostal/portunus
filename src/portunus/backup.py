"""Coordinated multi-file snapshot -- the foundation for `portunus vault
export`/`import` (portunus-vault-backup story 03). This module (story 02) is
infrastructure only: a primitive that reads the vault's critical-state
surface as one consistent instant, nothing else.

Acquires every lock protecting that surface -- registry.lock,
vault.enc.lock (guards both master.key and vault.enc.json together, the
same pairing LocalEncryptedBackend itself protects as one file's worth of
state), and vault-bindings.lock -- plus audit.log's own .clock.lock, in a
FIXED order (sorted by lock filename, never any other order, anywhere this
primitive is used) to prevent a lock-ordering deadlock against any future
caller that needs more than one of these locks at once. Reads every present
target file's raw bytes while holding every lock simultaneously, then
releases. The result reflects one consistent instant -- never independent
reads straddling a concurrent writer's mutation.

gcp-bindings.json (legacy) and rotation-bindings.json are read WITHOUT a
lock: neither has a dedicated writer-side lock today (this epic adds one for
vault-bindings.json only, per its own confirmed scope -- see
design-discussion.md §3), so there is no lock to acquire for either. Reading
either unlocked is the same posture this codebase already accepts for every
other unlocked read: os.replace()'s atomicity means a reader never observes
a torn write, only possibly-stale content, which is what a legacy/optional
file already tolerates.
"""
from __future__ import annotations

from contextlib import ExitStack, contextmanager
from pathlib import Path
from typing import Dict, List, Optional

from .filelock import flock_path
from .paths import home

# filename -> lock filename, for every file this snapshot serializes against
# before reading. Dict iteration order here is irrelevant to correctness --
# snapshot() derives the one fixed acquisition order itself (sorted by lock
# filename), so there is exactly one order, defined in exactly one place.
LOCKED_FILES: Dict[str, str] = {
    "registry.json": "registry.lock",
    "master.key": "vault.enc.lock",
    "vault.enc.json": "vault.enc.lock",
    "vault-bindings.json": "vault-bindings.lock",
    "audit.log": ".clock.lock",
}

# Read as-is, without a lock -- see module docstring.
UNLOCKED_FILES: List[str] = ["gcp-bindings.json", "rotation-bindings.json"]


@contextmanager
def _acquire_all(lock_paths: List[Path]):
    """Acquire every lock in `lock_paths` (already caller-sorted into the
    one fixed order), all-or-nothing, via a single ExitStack -- if any
    acquisition times out, every lock already held is released before the
    exception propagates."""
    with ExitStack() as stack:
        for lock_path in lock_paths:
            stack.enter_context(flock_path(lock_path))
        yield


def snapshot(base: Optional[Path] = None) -> Dict[str, bytes]:
    """Return {filename: raw_bytes} for every present critical-state file,
    taken as one consistent instant under every relevant lock held
    simultaneously. Missing optional files (legacy gcp-bindings.json,
    rotation-bindings.json, or an as-yet-unused master.key/vault.enc.json on
    a fresh vault) are simply omitted -- never an error."""
    base = Path(base) if base else home()
    lock_filenames = sorted(set(LOCKED_FILES.values()))
    lock_paths = [base / name for name in lock_filenames]

    result: Dict[str, bytes] = {}
    with _acquire_all(lock_paths):
        for filename in LOCKED_FILES:
            path = base / filename
            if path.exists():
                result[filename] = path.read_bytes()
    for filename in UNLOCKED_FILES:
        path = base / filename
        if path.exists():
            result[filename] = path.read_bytes()
    return result
