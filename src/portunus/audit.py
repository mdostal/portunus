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

import hashlib
import json
import os
from pathlib import Path
from typing import List, Optional

from .paths import home


class AuditChain:
    def __init__(self, path: Optional[Path] = None, clock_path: Optional[Path] = None):
        base = home()
        self.path = Path(path) if path else base / "audit.log"
        self.clock_path = Path(clock_path) if clock_path else base / ".clock"
        if not self.path.exists():
            self.path.touch()
            os.chmod(self.path, 0o600)

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
