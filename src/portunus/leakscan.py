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
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Callable, Dict, List, Optional, Tuple

from .backend import BackendError, SecretBackend
from .broker import ApprovalRequired, Broker, NotInjectable
from .registry import Registry

MIN_SEARCHABLE_VALUE_LENGTH = 8


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
