"""Tamper-evident audit chain.

Every access decision (resolve / grant / gate / approve / deny) appends one
line whose SHA-256 covers the previous line's hash plus this event. Any edit
or deletion breaks the chain, which ``verify()`` detects. Ported from the
hash-chain in ``bin/secrets``.

A monotonic counter (a file in the state home) supplies ``seq`` so the chain
is deterministic and testable without a wall clock.

Crucially: an audit entry records the *reference name* and *SM name* only —
never a secret value.
"""
from __future__ import annotations

import fcntl
import hashlib
import json
import os
import time
from contextlib import contextmanager
from pathlib import Path
from typing import List, Optional

from .paths import home

_LOCK_POLL_INTERVAL = 0.05
_LOCK_TIMEOUT = 10.0


class AuditChain:
    def __init__(self, path: Optional[Path] = None, clock_path: Optional[Path] = None):
        base = home()
        self.path = Path(path) if path else base / "audit.log"
        self.clock_path = Path(clock_path) if clock_path else base / ".clock"
        self.lock_path = self.clock_path.with_suffix(".lock")
        if not self.path.exists():
            self.path.touch()
            os.chmod(self.path, 0o600)

    @contextmanager
    def _locked(self):
        """Serializes append() across processes/threads sharing this audit
        log -- append() does a read-modify-write on the sequence counter
        AND reads the prior entry's hash before writing its own, so the
        whole operation must be atomic, not just the counter increment.
        Confirmed via a real reproduction (two concurrent `portunus
        resolve` calls) that an unlocked version of this can race and
        produce a duplicate seq, breaking the hash chain -- same flock
        idiom Registry._locked() already uses."""
        self.lock_path.parent.mkdir(parents=True, exist_ok=True)
        fh = open(self.lock_path, "w")
        deadline = time.monotonic() + _LOCK_TIMEOUT
        acquired = False
        try:
            while time.monotonic() < deadline:
                try:
                    fcntl.flock(fh, fcntl.LOCK_EX | fcntl.LOCK_NB)
                    acquired = True
                    break
                except OSError:
                    time.sleep(_LOCK_POLL_INTERVAL)
            if not acquired:
                raise TimeoutError(
                    f"could not acquire audit lock within {_LOCK_TIMEOUT}s ({self.lock_path})"
                )
            yield
        finally:
            if acquired:
                fcntl.flock(fh, fcntl.LOCK_UN)
            fh.close()

    def _tick(self) -> int:
        try:
            cur = int(self.clock_path.read_text().strip() or "0")
        except (OSError, ValueError):
            cur = 0
        nxt = cur + 1
        self.clock_path.write_text(str(nxt))
        os.chmod(self.clock_path, 0o600)
        return nxt

    def _last_hash(self) -> str:
        last = "genesis"
        try:
            with self.path.open() as fh:
                for line in fh:
                    line = line.strip()
                    if not line:
                        continue
                    try:
                        last = json.loads(line)["h"]
                    except (json.JSONDecodeError, KeyError):
                        continue
        except OSError:
            pass
        return last

    def append(self, action: str, secret: str, result: str,
               actor: Optional[str] = None, task: Optional[str] = None) -> dict:
        """Append one audit event. `secret` is a reference/SM name, never a value."""
        actor = actor or os.environ.get("DOSTAL_AGENT") or os.environ.get("USER", "unknown")
        task = task if task is not None else os.environ.get("DOSTAL_TASK", "")
        with self._locked():
            seq = self._tick()
            prev = self._last_hash()
            # Fixed key order so verify() can recompute the body byte-for-byte.
            body = json.dumps(
                {"seq": seq, "actor": actor, "task": task, "action": action,
                 "secret": secret, "result": result, "prev": prev},
                separators=(",", ":"), sort_keys=False,
            )
            digest = hashlib.sha256((prev + body).encode()).hexdigest()
            entry = json.loads(body)
            entry["h"] = digest
            with self.path.open("a") as fh:
                fh.write(json.dumps(entry, separators=(",", ":"), sort_keys=False) + "\n")
        return entry

    def entries(self) -> List[dict]:
        out: List[dict] = []
        try:
            with self.path.open() as fh:
                for line in fh:
                    line = line.strip()
                    if line:
                        out.append(json.loads(line))
        except OSError:
            pass
        return out

    def verify(self) -> bool:
        """Return True iff the hash chain is intact."""
        prev = "genesis"
        for entry in self.entries():
            body = json.dumps(
                {k: entry[k] for k in
                 ("seq", "actor", "task", "action", "secret", "result", "prev")},
                separators=(",", ":"), sort_keys=False,
            )
            calc = hashlib.sha256((entry["prev"] + body).encode()).hexdigest()
            if entry["prev"] != prev or calc != entry.get("h"):
                return False
            prev = entry["h"]
        return True
