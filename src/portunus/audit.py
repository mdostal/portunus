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
from typing import Iterable, List, Optional, Tuple

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

    def _read_clock(self) -> Optional[int]:
        """The counter file's value, or None when missing/unparsable."""
        try:
            return int(self.clock_path.read_text().strip())
        except (OSError, ValueError):
            return None

    def _scan(self) -> Tuple[int, str]:
        """One pass over the log: (highest seq, hash of the last parseable
        entry). Corrupt lines are skipped here -- check() reports them."""
        max_seq, last = 0, "genesis"
        for _, entry, _ in _parse_lines(self.path):
            if entry is None:
                continue
            seq = entry.get("seq")
            if isinstance(seq, int) and not isinstance(seq, bool):
                max_seq = max(max_seq, seq)
            if isinstance(entry.get("h"), str):
                last = entry["h"]
        return max_seq, last

    def current_seq(self, log_max_seq: Optional[int] = None) -> int:
        """The last issued seq. A missing, corrupt or rolled-back ``.clock``
        is recovered from the highest ``seq`` in the log, so the counter
        never goes backwards while the log has entries."""
        if log_max_seq is None:
            log_max_seq = self._scan()[0]
        return max(self._read_clock() or 0, log_max_seq)

    def _tick(self, log_max_seq: Optional[int] = None) -> int:
        nxt = self.current_seq(log_max_seq) + 1
        self.clock_path.write_text(str(nxt))
        os.chmod(self.clock_path, 0o600)
        return nxt

    def _last_hash(self) -> str:
        return self._scan()[1]

    def append(self, action: str, secret: str, result: str,
               actor: Optional[str] = None, task: Optional[str] = None) -> dict:
        """Append one audit event. `secret` is a reference/SM name, never a value."""
        actor = actor or os.environ.get("DOSTAL_AGENT") or os.environ.get("USER", "unknown")
        task = task if task is not None else os.environ.get("DOSTAL_TASK", "")
        with self._locked():
            max_seq, prev = self._scan()
            seq = self._tick(max_seq)
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
        """Parseable entries in log order. Corrupt lines are skipped rather
        than raised; ``check()`` / ``verify()`` report them."""
        return [e for _, e, _ in _parse_lines(self.path) if e is not None]

    def check(self) -> dict:
        """Structured chain verification that never raises:
        ``{"ok", "entries", "line", "reason"}`` where ``line`` is the
        1-based line number of the first bad line (None when intact)."""
        return check_lines(_parse_lines(self.path))

    def verify(self) -> bool:
        """Return True iff the hash chain is intact."""
        return self.check()["ok"]


_CHAIN_FIELDS = ("seq", "actor", "task", "action", "secret", "result", "prev")

_Line = Tuple[int, Optional[dict], str]


def _parse_lines(path: Path) -> List[_Line]:
    """(line number, parsed entry or None, parse error) per non-blank line."""
    out: List[_Line] = []
    try:
        with Path(path).open() as fh:
            for n, line in enumerate(fh, start=1):
                line = line.strip()
                if not line:
                    continue
                try:
                    entry = json.loads(line)
                except ValueError as exc:
                    out.append((n, None, f"unparseable JSON: {exc}"))
                    continue
                if not isinstance(entry, dict):
                    out.append((n, None, "entry is not a JSON object"))
                    continue
                out.append((n, entry, ""))
    except OSError:
        pass
    return out


def _entry_problem(entry: dict, prev: str) -> str:
    """Why `entry` does not chain onto `prev` ("" when it does)."""
    if not all(k in entry for k in _CHAIN_FIELDS) or not isinstance(entry["prev"], str):
        return "entry is missing chain fields"
    if entry["prev"] != prev:
        return "prev hash does not match the preceding entry"
    body = json.dumps({k: entry[k] for k in _CHAIN_FIELDS},
                      separators=(",", ":"), sort_keys=False)
    calc = hashlib.sha256((entry["prev"] + body).encode()).hexdigest()
    if calc != entry.get("h"):
        return "hash mismatch (entry was modified)"
    return ""


def check_lines(lines: Iterable[_Line]) -> dict:
    """Walk (line number, entry, parse error) triples and report the first
    line that breaks the chain. Never raises on malformed input."""
    prev, count = "genesis", 0
    for n, entry, err in lines:
        if entry is None:
            return {"ok": False, "entries": count, "line": n, "reason": err}
        problem = _entry_problem(entry, prev)
        if problem:
            return {"ok": False, "entries": count, "line": n, "reason": problem}
        prev = entry["h"]
        count += 1
    return {"ok": True, "entries": count, "line": None, "reason": ""}


def verify_entries(entries: List[dict]) -> bool:
    """Return True iff `entries` form an intact hash chain. A free function
    so `portunus health` can verify a log without constructing an
    AuditChain, whose constructor creates/chmods the state home."""
    return check_lines(
        (n, e if isinstance(e, dict) else None, "entry is not a JSON object")
        for n, e in enumerate(entries, start=1)
    )["ok"]
