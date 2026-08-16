"""portunus leak-scan -- detects whether a managed secret's actual decrypted
value shows up somewhere it shouldn't (logs, .claude conversation
transcripts, shell history, or any other human-configured local path).

This is the single strictest instance of the secret-boundary-invariant in
this codebase: unlike every other module that touches a decrypted value,
this one must CALL Backend.access() to get values to search FOR, and then
guarantee those values never escape beyond an in-memory per-line
comparison. Findings carry only {ref_name, path, line_number, byte_offset}
-- never a value, never a matched substring, never a context excerpt.
Values live only in local variables inside this module's own functions;
nothing here ever prints, logs, or returns one, and a mid-scan failure is
never allowed to surface one via an exception (see
test_search_error_never_leaks_value_in_exception).

Scanning is line-based, not chunk-based (design-discussion.md Sec 0): it
gives line numbers for free and sidesteps chunk-boundary-match bugs, at the
accepted cost of not catching a value split across two lines. Every real
target path identified during planning (.claude JSONL transcripts, shell
history, most logs) is inherently line-oriented, so this is a natural fit,
not a contrived one. A trailing line with no newline yet (a file still
being actively written to) is never counted as consumed -- it's re-read in
full on the next scan once it's complete.

Incremental: a per-file Watermark (byte offset + a (size, mtime)
fingerprint + how many complete lines were consumed) means a re-scan only
reads newly-appended bytes. A file whose current (size, mtime) is
inconsistent with growth-only is treated as a different file in spirit --
rescanned from byte 0 (matches log-rotation-aware tooling's usual caution).

Values shorter than MIN_SEARCHABLE_VALUE_LENGTH are never fetched into the
search set at all -- see design-discussion.md Sec 5 for why (a
trivial-length value would false-positive-match constantly, burning the
scan's time budget on noise instead of signal).
"""
from __future__ import annotations

import glob as glob_module
import json
import os
import re
import subprocess
import tempfile
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable, Dict, List, Optional, Tuple

from .backend import BackendError, SecretBackend
from .broker import ApprovalRequired, Broker, NotInjectable
from .filelock import flock_path
from .paths import home
from .registry import Registry

MIN_SEARCHABLE_VALUE_LENGTH = 8

# Escalation ladder (design-discussion.md §3) -- days since the EARLIEST
# first_detected_at across a reference's active findings.
WARN_TO_URGENT_DAYS = 3
URGENT_TO_CRITICAL_DAYS = 7
_SECONDS_PER_DAY = 86400.0


@dataclass(frozen=True)
class Finding:
    """A single location where a managed secret's value was found outside
    the vault. Deliberately narrow: no field here is capable of holding a
    value or a substring of one."""

    ref_name: str
    path: str
    line_number: int
    byte_offset: int


@dataclass(frozen=True)
class Watermark:
    """How far a given file has already been scanned. `size`/`mtime` are a
    fingerprint used to detect a shrunk or replaced file, in which case the
    file is rescanned from byte 0 rather than trusting a stale offset.
    `line_count` is how many COMPLETE lines were consumed as of `offset` --
    a trailing line with no newline yet is never counted."""

    offset: int
    size: int
    mtime: float
    line_count: int


def get_values(
    registry: Registry,
    broker: Broker,
    backend: SecretBackend,
    backend_for: Optional[Callable[[object], SecretBackend]] = None,
) -> Dict[str, str]:
    """Fetch the current decrypted value for every reference that is
    currently injectable (the same check_injectable() gate resolve/inject
    already use) and long enough to search for safely.

    Returns a plain dict scoped to the caller's own stack frame -- callers
    must not persist this dict or return it beyond the scan it's used for.
    """
    values: Dict[str, str] = {}
    for ref in registry:
        try:
            gated = broker.check_injectable(ref.name)
        except (NotInjectable, ApprovalRequired):
            continue
        chosen_backend = backend_for(gated) if backend_for is not None else backend
        try:
            value = chosen_backend.access(gated.sm_name, project=gated.project)
        except BackendError:
            continue
        if len(value) < MIN_SEARCHABLE_VALUE_LENGTH:
            continue
        values[gated.name] = value
    return values


def _compile_pattern(values: Dict[str, str]):
    """One compiled alternation over every distinct value, longest first so
    a value that happens to be a substring of another doesn't shadow it at
    the same match position. Returns (pattern, value_to_names) or
    (None, {}) when there's nothing to search for."""
    if not values:
        return None, {}
    value_to_names: Dict[str, List[str]] = {}
    for name, value in values.items():
        value_to_names.setdefault(value, []).append(name)
    ordered = sorted(value_to_names.keys(), key=len, reverse=True)
    pattern = re.compile("|".join(re.escape(v) for v in ordered))
    return pattern, value_to_names


def _expand_paths(path_globs: List[str]) -> List[Path]:
    files: List[Path] = []
    seen = set()
    for pattern in path_globs:
        for match in glob_module.glob(pattern, recursive=True):
            p = Path(match)
            if p.is_file() and p not in seen:
                seen.add(p)
                files.append(p)
    return sorted(files)


def _scan_one_file(
    path: Path, pattern, value_to_names: Dict[str, List[str]], watermark: Optional[Watermark]
) -> Tuple[List[Finding], Watermark]:
    try:
        stat_result = path.stat()
    except OSError:
        fallback = watermark or Watermark(offset=0, size=0, mtime=0.0, line_count=0)
        return [], fallback

    size, mtime = stat_result.st_size, stat_result.st_mtime

    if watermark is not None and size >= watermark.size and mtime >= watermark.mtime:
        start_offset = watermark.offset
        line_count = watermark.line_count
    else:
        start_offset = 0
        line_count = 0

    findings: List[Finding] = []
    offset = start_offset
    consumed_offset = start_offset
    consumed_line_count = line_count

    try:
        with path.open("rb") as fh:
            fh.seek(start_offset)
            while True:
                line_bytes = fh.readline()
                if not line_bytes:
                    break
                complete = line_bytes.endswith(b"\n")
                if not complete:
                    break  # actively-being-written trailing line -- leave for next scan
                line_count += 1
                text = line_bytes.decode("utf-8", errors="replace")
                for match in pattern.finditer(text):
                    for name in value_to_names.get(match.group(0), []):
                        findings.append(Finding(name, str(path), line_count, offset))
                offset += len(line_bytes)
                consumed_offset = offset
                consumed_line_count = line_count
    except OSError:
        pass

    new_watermark = Watermark(
        offset=consumed_offset, size=size, mtime=mtime, line_count=consumed_line_count
    )
    return findings, new_watermark


def scan_paths(
    path_globs: List[str],
    values: Dict[str, str],
    watermarks: Dict[str, Watermark],
) -> Tuple[List[Finding], Dict[str, Watermark]]:
    """Search every file matched by `path_globs` for literal occurrences of
    `values` (ref_name -> value), honoring each file's prior watermark.

    Returns (findings, updated_watermarks) -- updated_watermarks contains
    an entry for every file actually scanned this call; the caller is
    responsible for merging it into whatever it persists. Never mutates
    the `watermarks` argument in place.
    """
    pattern, value_to_names = _compile_pattern(values)
    if pattern is None:
        return [], {}

    all_findings: List[Finding] = []
    new_watermarks: Dict[str, Watermark] = {}
    for file_path in _expand_paths(path_globs):
        key = str(file_path)
        prior = watermarks.get(key)
        findings, watermark = _scan_one_file(file_path, pattern, value_to_names, prior)
        all_findings.extend(findings)
        new_watermarks[key] = watermark

    return all_findings, new_watermarks


# ---------------------------------------------------------------------------
# Leak-status store (portunus-leak-scan Slice 2) -- persisted escalation
# state, locked from day one (matching views.py/roles.py), never a value.
# ---------------------------------------------------------------------------


class LeakScanError(RuntimeError):
    """Raised for a leak-status operation that can't complete. Never
    carries a secret value -- this store only ever holds ref
    names/paths/line numbers/timestamps."""


@dataclass
class LeakFinding:
    path: str
    line_number: int
    first_detected_at: float
    last_detected_at: float


@dataclass
class LeakStatus:
    ref_name: str
    findings: List[LeakFinding] = field(default_factory=list)
    rotated_at: Optional[float] = None


def _leak_status_path(path: Optional[Path] = None) -> Path:
    return path or (home() / "leak-status.json")


def _leak_status_lock_path(path: Optional[Path] = None) -> Path:
    return _leak_status_path(path).with_suffix(".lock")


def _load_status_unlocked(path: Optional[Path] = None) -> Dict[str, LeakStatus]:
    status_path = _leak_status_path(path)
    if not status_path.exists():
        return {}
    raw = json.loads(status_path.read_text() or "{}")
    statuses: Dict[str, LeakStatus] = {}
    for ref_name, cfg in raw.items():
        statuses[ref_name] = LeakStatus(
            ref_name=ref_name,
            findings=[
                LeakFinding(
                    path=f["path"],
                    line_number=f["line_number"],
                    first_detected_at=f["first_detected_at"],
                    last_detected_at=f["last_detected_at"],
                )
                for f in cfg.get("findings", [])
            ],
            rotated_at=cfg.get("rotated_at"),
        )
    return statuses


def _save_status_unlocked(statuses: Dict[str, LeakStatus], path: Optional[Path] = None) -> None:
    status_path = _leak_status_path(path)
    status_path.parent.mkdir(parents=True, exist_ok=True)
    raw = {
        ref_name: {
            "findings": [
                {
                    "path": f.path,
                    "line_number": f.line_number,
                    "first_detected_at": f.first_detected_at,
                    "last_detected_at": f.last_detected_at,
                }
                for f in status.findings
            ],
            "rotated_at": status.rotated_at,
        }
        for ref_name, status in statuses.items()
    }
    tmp = status_path.with_suffix(".json.tmp")
    tmp.write_text(json.dumps(raw, indent=2))
    os.chmod(tmp, 0o600)
    os.replace(tmp, status_path)
    os.chmod(status_path, 0o600)


def load_leak_status(path: Optional[Path] = None) -> Dict[str, LeakStatus]:
    """Plain read -- missing file means no findings recorded yet, returns
    {}. Unlocked, matching every other config-load's own posture in this
    codebase (a reader never observes a torn write; os.replace() is
    atomic)."""
    return _load_status_unlocked(path)


def record_findings(
    findings: List[Finding], now: Optional[float] = None, path: Optional[Path] = None
) -> Dict[str, LeakStatus]:
    """Persist `findings` (from scan_paths()) into the leak-status store.
    Re-detecting the same (ref_name, path, line_number) updates
    last_detected_at in place rather than duplicating. Locked for the
    entire load -> mutate -> save, matching views.py's own from-day-one
    discipline. Returns the full, updated store."""
    ts = now if now is not None else time.time()
    with flock_path(_leak_status_lock_path(path)):
        statuses = _load_status_unlocked(path)
        for finding in findings:
            status = statuses.setdefault(finding.ref_name, LeakStatus(ref_name=finding.ref_name))
            existing = next(
                (
                    f
                    for f in status.findings
                    if f.path == finding.path and f.line_number == finding.line_number
                ),
                None,
            )
            if existing is not None:
                existing.last_detected_at = ts
            else:
                status.findings.append(
                    LeakFinding(
                        path=finding.path,
                        line_number=finding.line_number,
                        first_detected_at=ts,
                        last_detected_at=ts,
                    )
                )
        _save_status_unlocked(statuses, path)
        return statuses


def mark_rotated(
    ref_name: str,
    now: Optional[float] = None,
    path: Optional[Path] = None,
    watermarks_path: Optional[Path] = None,
) -> None:
    """A human's own assertion that `ref_name` has been rotated at its
    provider -- Portunus cannot independently verify this
    (design-discussion.md §7). Clears active findings and resets the
    escalation clock, AND invalidates the watermark for every file where
    this reference had a finding -- so a rescan genuinely re-reads those
    bytes rather than trusting a watermark that already scanned past them.
    Without this, "a rescan will naturally re-flag a premature
    mark-rotated" would be a documented promise the incremental watermark
    silently broke. A harmless no-op for a reference with no active
    findings."""
    ts = now if now is not None else time.time()
    with flock_path(_leak_status_lock_path(path)):
        statuses = _load_status_unlocked(path)
        if ref_name not in statuses or not statuses[ref_name].findings:
            return
        affected_paths = {f.path for f in statuses[ref_name].findings}
        statuses[ref_name] = LeakStatus(ref_name=ref_name, findings=[], rotated_at=ts)
        _save_status_unlocked(statuses, path)

    _invalidate_watermarks_for_paths(affected_paths, watermarks_path)


def severity(status: LeakStatus, now: Optional[float] = None) -> Optional[str]:
    """warn/urgent/critical, derived at read time from elapsed time since
    the EARLIEST first_detected_at across all of `status`'s active
    findings -- never stored redundantly (design-discussion.md §3). None
    when there are no active findings."""
    if not status.findings:
        return None
    ts = now if now is not None else time.time()
    earliest = min(f.first_detected_at for f in status.findings)
    elapsed_days = (ts - earliest) / _SECONDS_PER_DAY
    if elapsed_days < WARN_TO_URGENT_DAYS:
        return "warn"
    if elapsed_days < URGENT_TO_CRITICAL_DAYS:
        return "urgent"
    return "critical"


def summarize(
    status: LeakStatus, now: Optional[float] = None, detail: bool = False
) -> Dict[str, object]:
    """{ref_name, severity, finding_count, first_detected_at,
    last_detected_at} -- the one computed shape both the CLI (Slice 3) and
    the read-only MCP tool (Slice 4) render, never duplicated.

    `detail=True` (portunus-leak-visibility Story 01) additionally includes
    `findings` (the raw path/line_number/timestamps list -- still never a
    value) and `distinct_files` (unique file paths, NOT raw finding count --
    a transcript can match the same secret on many lines, which shouldn't
    inflate "how many places did this leak" beyond how many actual files
    are involved). Defaults to False so every existing caller's shape is
    unchanged -- no accidental behavior change."""
    if not status.findings:
        base: Dict[str, object] = {
            "ref_name": status.ref_name,
            "severity": None,
            "finding_count": 0,
            "first_detected_at": None,
            "last_detected_at": None,
        }
        if detail:
            base["findings"] = []
            base["distinct_files"] = 0
        return base

    base = {
        "ref_name": status.ref_name,
        "severity": severity(status, now=now),
        "finding_count": len(status.findings),
        "first_detected_at": min(f.first_detected_at for f in status.findings),
        "last_detected_at": max(f.last_detected_at for f in status.findings),
    }
    if detail:
        base["findings"] = [
            {
                "path": f.path,
                "line_number": f.line_number,
                "first_detected_at": f.first_detected_at,
                "last_detected_at": f.last_detected_at,
            }
            for f in status.findings
        ]
        base["distinct_files"] = len({f.path for f in status.findings})
    return base


# ---------------------------------------------------------------------------
# Scan-path config store (portunus-leak-scan Slice 3) -- explicit, persisted,
# empty by default. Its own lock file, separate from leak-status.json's
# higher-churn writes (design-discussion.md self-grill).
# ---------------------------------------------------------------------------


def _scan_config_path(path: Optional[Path] = None) -> Path:
    return path or (home() / "leak-scan-config.json")


def _scan_config_lock_path(path: Optional[Path] = None) -> Path:
    return _scan_config_path(path).with_suffix(".lock")


def load_scan_paths(path: Optional[Path] = None) -> List[str]:
    """Missing file means nothing configured yet -- returns []. `leak-scan`
    with an empty config says so explicitly rather than silently
    succeeding at having scanned nothing."""
    config_path = _scan_config_path(path)
    if not config_path.exists():
        return []
    raw = json.loads(config_path.read_text() or "{}")
    return list(raw.get("paths", []))


def _save_scan_paths_unlocked(paths: List[str], path: Optional[Path] = None) -> None:
    config_path = _scan_config_path(path)
    config_path.parent.mkdir(parents=True, exist_ok=True)
    tmp = config_path.with_suffix(".json.tmp")
    tmp.write_text(json.dumps({"paths": paths}, indent=2))
    os.chmod(tmp, 0o600)
    os.replace(tmp, config_path)
    os.chmod(config_path, 0o600)


def add_scan_path(glob_pattern: str, path: Optional[Path] = None) -> List[str]:
    with flock_path(_scan_config_lock_path(path)):
        paths = load_scan_paths(path)
        if glob_pattern not in paths:
            paths.append(glob_pattern)
            _save_scan_paths_unlocked(paths, path)
        return paths


def remove_scan_path(glob_pattern: str, path: Optional[Path] = None) -> List[str]:
    with flock_path(_scan_config_lock_path(path)):
        paths = load_scan_paths(path)
        if glob_pattern in paths:
            paths.remove(glob_pattern)
            _save_scan_paths_unlocked(paths, path)
        return paths


# ---------------------------------------------------------------------------
# Git-repository scan targets (portunus-leak-scan-git-awareness Story 01) --
# its own store/lock, explicit and persisted, empty by default, matching
# leak-scan-config.json's own "nothing scanned until a human adds it" posture.
# ---------------------------------------------------------------------------


def _repos_path(path: Optional[Path] = None) -> Path:
    return path or (home() / "leak-scan-repos.json")


def _repos_lock_path(path: Optional[Path] = None) -> Path:
    return _repos_path(path).with_suffix(".lock")


def load_scan_repos(path: Optional[Path] = None) -> List[str]:
    repos_path = _repos_path(path)
    if not repos_path.exists():
        return []
    raw = json.loads(repos_path.read_text() or "{}")
    return list(raw.get("repos", []))


def _save_scan_repos_unlocked(repos: List[str], path: Optional[Path] = None) -> None:
    repos_path = _repos_path(path)
    repos_path.parent.mkdir(parents=True, exist_ok=True)
    tmp = repos_path.with_suffix(".json.tmp")
    tmp.write_text(json.dumps({"repos": repos}, indent=2))
    os.chmod(tmp, 0o600)
    os.replace(tmp, repos_path)
    os.chmod(repos_path, 0o600)


def add_scan_repo(repo_path: str, path: Optional[Path] = None) -> List[str]:
    with flock_path(_repos_lock_path(path)):
        repos = load_scan_repos(path)
        if repo_path not in repos:
            repos.append(repo_path)
            _save_scan_repos_unlocked(repos, path)
        return repos


def remove_scan_repo(repo_path: str, path: Optional[Path] = None) -> List[str]:
    with flock_path(_repos_lock_path(path)):
        repos = load_scan_repos(path)
        if repo_path in repos:
            repos.remove(repo_path)
            _save_scan_repos_unlocked(repos, path)
        return repos


def _dump_git_history(repo_path: str) -> Optional[Path]:
    """Dump a repo's full history (every branch, full diffs, OLDEST first)
    to a fresh temp file for scan_paths() to read unchanged -- no new
    matching engine, reusing exactly the mechanism a manual verification
    already proved works end to end. Oldest-first (--reverse) so that
    adding new commits APPENDS to the dump rather than shifting every
    existing line's number -- keeps (path, line_number) dedup keys stable
    across repeated scans of an actively-developed repo. A rebase/force-
    push can still rewrite history in ways this doesn't protect against --
    an accepted, documented limitation (design-discussion.md §2), not
    silently assumed away.

    Returns None (not an error) if the path isn't a git repo or git isn't
    available -- a misconfigured repo entry never crashes the whole scan
    run."""
    if not Path(repo_path).is_dir():
        return None
    try:
        result = subprocess.run(
            ["git", "-C", repo_path, "log", "--all", "-p", "--full-history", "--reverse"],
            capture_output=True, text=True, timeout=120,
        )
    except (OSError, subprocess.TimeoutExpired):
        return None
    if result.returncode != 0:
        return None
    fd, tmp_path = tempfile.mkstemp(prefix="portunus-leak-scan-repo-")
    with os.fdopen(fd, "w") as fh:
        fh.write(result.stdout)
    return Path(tmp_path)


def _scan_repo_history(repo_path: str, values: Dict[str, str]) -> List[Finding]:
    """Scan one configured repo's full git history for occurrences of
    `values`. Always a full re-scan -- git history can be rewritten, which
    makes a byte-offset watermark (correct for append-only log files)
    unsafe here (design-discussion.md §2). The temp dump is always deleted
    before this function returns, success or failure. Findings are
    remapped from the (ephemeral, fresh-every-run) temp file path to a
    stable `"<repo> (git history)"` label so record_findings()'s dedup key
    stays meaningful across runs."""
    dump_path = _dump_git_history(repo_path)
    if dump_path is None:
        return []
    try:
        findings, _ = scan_paths([str(dump_path)], values, {})
    finally:
        dump_path.unlink(missing_ok=True)
    stable_label = f"{repo_path} (git history)"
    return [
        Finding(
            ref_name=f.ref_name, path=stable_label,
            line_number=f.line_number, byte_offset=f.byte_offset,
        )
        for f in findings
    ]


# ---------------------------------------------------------------------------
# Watermark persistence (portunus-leak-scan Slice 3) -- its own lock file,
# rewritten on every scan (highest churn of the three stores).
# ---------------------------------------------------------------------------


def _watermarks_path(path: Optional[Path] = None) -> Path:
    return path or (home() / "leak-scan-watermarks.json")


def _watermarks_lock_path(path: Optional[Path] = None) -> Path:
    return _watermarks_path(path).with_suffix(".lock")


def load_watermarks(path: Optional[Path] = None) -> Dict[str, Watermark]:
    watermarks_path = _watermarks_path(path)
    if not watermarks_path.exists():
        return {}
    raw = json.loads(watermarks_path.read_text() or "{}")
    return {
        key: Watermark(
            offset=w["offset"], size=w["size"], mtime=w["mtime"], line_count=w["line_count"]
        )
        for key, w in raw.items()
    }


def _save_watermarks_unlocked(watermarks: Dict[str, Watermark], path: Optional[Path] = None) -> None:
    watermarks_path = _watermarks_path(path)
    watermarks_path.parent.mkdir(parents=True, exist_ok=True)
    raw = {
        key: {"offset": w.offset, "size": w.size, "mtime": w.mtime, "line_count": w.line_count}
        for key, w in watermarks.items()
    }
    tmp = watermarks_path.with_suffix(".json.tmp")
    tmp.write_text(json.dumps(raw, indent=2))
    os.chmod(tmp, 0o600)
    os.replace(tmp, watermarks_path)
    os.chmod(watermarks_path, 0o600)


def save_watermarks(watermarks: Dict[str, Watermark], path: Optional[Path] = None) -> None:
    with flock_path(_watermarks_lock_path(path)):
        _save_watermarks_unlocked(watermarks, path)


def _invalidate_watermarks_for_paths(
    file_paths: "set[str]", watermarks_path: Optional[Path] = None
) -> None:
    """Drop the watermark entry for each of `file_paths`, so the NEXT scan
    re-reads those specific files from byte 0 instead of trusting a
    watermark that already scanned past their content. Used by
    mark_rotated() to make good on its own documented promise (design-
    discussion.md §7): a rescan after a premature mark-rotated must
    actually be able to re-detect the old value, which an untouched
    watermark would silently prevent."""
    if not file_paths:
        return
    with flock_path(_watermarks_lock_path(watermarks_path)):
        watermarks = load_watermarks(watermarks_path)
        if not any(p in watermarks for p in file_paths):
            return
        for p in file_paths:
            watermarks.pop(p, None)
        _save_watermarks_unlocked(watermarks, watermarks_path)


# ---------------------------------------------------------------------------
# Orchestration (portunus-leak-scan Slice 3) -- ties get_values() +
# scan_paths() + the three stores together into one call a thin CLI/MCP
# layer can use without re-implementing the wiring.
# ---------------------------------------------------------------------------


@dataclass
class ScanRunResult:
    configured_paths: List[str]
    configured_repos: List[str]
    findings: List[Finding]


def run_scan(
    registry: Registry,
    broker: Broker,
    backend: SecretBackend,
    backend_for: Optional[Callable[[object], SecretBackend]] = None,
    now: Optional[float] = None,
    config_path: Optional[Path] = None,
    status_path: Optional[Path] = None,
    watermarks_path: Optional[Path] = None,
    repos_path: Optional[Path] = None,
) -> ScanRunResult:
    """The full scan pipeline: load configured paths/repos, fetch
    resolvable values, scan (incrementally for paths, always-full for
    repos -- design-discussion.md §2), persist new findings and
    watermarks. Both `configured_paths` and `configured_repos` empty is
    the signal callers use to report "nothing configured" explicitly
    rather than a silent empty-success."""
    configured_paths = load_scan_paths(config_path)
    configured_repos = load_scan_repos(repos_path)
    if not configured_paths and not configured_repos:
        return ScanRunResult(configured_paths=[], configured_repos=[], findings=[])

    values = get_values(registry, broker, backend, backend_for=backend_for)

    findings: List[Finding] = []
    if configured_paths:
        prior_watermarks = load_watermarks(watermarks_path)
        path_findings, new_watermarks = scan_paths(configured_paths, values, prior_watermarks)
        findings.extend(path_findings)
        merged_watermarks = {**prior_watermarks, **new_watermarks}
        save_watermarks(merged_watermarks, watermarks_path)

    for repo in configured_repos:
        findings.extend(_scan_repo_history(repo, values))

    if findings:
        record_findings(findings, now=now, path=status_path)

    return ScanRunResult(
        configured_paths=configured_paths, configured_repos=configured_repos, findings=findings
    )
