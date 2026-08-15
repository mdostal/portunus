"""leak-scan engine core (portunus-leak-scan Slice 1) -- incremental,
line-based matching. The single strictest instance of the secret-boundary-
invariant in this codebase: this module CALLS Backend.access() to get
values to search FOR, and must guarantee those values never escape beyond
an in-memory per-line comparison."""
import ast
import inspect
import time

import pytest

from portunus import Registry
from portunus.broker import Broker
from portunus.leakscan import (
    MIN_SEARCHABLE_VALUE_LENGTH,
    Watermark,
    get_values,
    scan_paths,
)


# ---------------------------------------------------------------------------
# get_values() -- fetching resolvable values, filtered and length-gated
# ---------------------------------------------------------------------------

def test_get_values_returns_resolvable_reference_value(stack):
    stack["registry"].add("x", "sm-x")
    stack["backend"].set("sm-x", "a-real-secret-value-123")
    values = get_values(stack["registry"], stack["broker"], stack["backend"])
    assert values == {"x": "a-real-secret-value-123"}


def test_get_values_skips_dropped_reference(stack):
    stack["registry"].add("x", "sm-x")
    stack["backend"].set("sm-x", "a-real-secret-value-123")
    stack["registry"].set_state("x", "dropped")
    values = get_values(stack["registry"], stack["broker"], stack["backend"])
    assert "x" not in values


def test_get_values_skips_requested_reference(stack):
    stack["registry"].add("x", "sm-x")
    stack["registry"].set_state("x", "requested")
    values = get_values(stack["registry"], stack["broker"], stack["backend"])
    assert "x" not in values


def test_get_values_skips_values_shorter_than_minimum(stack):
    short = "a" * (MIN_SEARCHABLE_VALUE_LENGTH - 1)
    stack["registry"].add("short", "sm-short")
    stack["backend"].set("sm-short", short)
    stack["registry"].add("long", "sm-long")
    stack["backend"].set("sm-long", "a-real-secret-value-123")
    values = get_values(stack["registry"], stack["broker"], stack["backend"])
    assert "short" not in values
    assert "long" in values


def test_get_values_boundary_length_is_searchable(stack):
    exact = "a" * MIN_SEARCHABLE_VALUE_LENGTH
    stack["registry"].add("x", "sm-x")
    stack["backend"].set("sm-x", exact)
    values = get_values(stack["registry"], stack["broker"], stack["backend"])
    assert "x" in values


# ---------------------------------------------------------------------------
# scan_paths() -- matching, incremental watermarks
# ---------------------------------------------------------------------------

def test_finds_literal_value_in_scanned_file(tmp_path):
    f = tmp_path / "transcript.jsonl"
    f.write_text("harmless line\ncontains SECRET-VALUE-abc123 right here\n")
    findings, _ = scan_paths([str(f)], {"x": "SECRET-VALUE-abc123"}, {})
    assert len(findings) == 1
    assert findings[0].ref_name == "x"
    assert findings[0].path == str(f)
    assert findings[0].line_number == 2


def test_no_match_when_value_absent(tmp_path):
    f = tmp_path / "clean.log"
    f.write_text("nothing sensitive here\njust normal log lines\n")
    findings, _ = scan_paths([str(f)], {"x": "SECRET-VALUE-abc123"}, {})
    assert findings == []


def test_no_duplicate_findings_on_unchanged_rescan(tmp_path):
    f = tmp_path / "log.txt"
    f.write_text("leak: SECRET-VALUE-abc123\n")
    values = {"x": "SECRET-VALUE-abc123"}
    findings1, watermarks1 = scan_paths([str(f)], values, {})
    assert len(findings1) == 1
    findings2, watermarks2 = scan_paths([str(f)], values, watermarks1)
    assert findings2 == []


def test_appended_content_scanned_incrementally(tmp_path):
    f = tmp_path / "log.txt"
    f.write_text("first leak: SECRET-VALUE-abc123\n")
    values = {"x": "SECRET-VALUE-abc123", "y": "OTHER-SECRET-VALUE-999"}
    findings1, watermarks1 = scan_paths([str(f)], values, {})
    assert [fnd.ref_name for fnd in findings1] == ["x"]

    with f.open("a") as fh:
        fh.write("second leak: OTHER-SECRET-VALUE-999\n")

    findings2, watermarks2 = scan_paths([str(f)], values, watermarks1)
    assert [fnd.ref_name for fnd in findings2] == ["y"]
    assert findings2[0].line_number == 2


def test_shrunk_or_replaced_file_rescans_from_zero(tmp_path):
    f = tmp_path / "log.txt"
    f.write_text("padding line one\npadding line two\nSECRET-VALUE-abc123\n")
    values = {"x": "SECRET-VALUE-abc123"}
    findings1, watermarks1 = scan_paths([str(f)], values, {})
    assert len(findings1) == 1

    # Simulate log rotation: file replaced with fresh, shorter content that
    # still contains the same secret -- must be re-detected, not skipped
    # because the stale watermark's offset now points past EOF.
    time.sleep(0.01)
    f.write_text("SECRET-VALUE-abc123\n")

    findings2, watermarks2 = scan_paths([str(f)], values, watermarks1)
    assert len(findings2) == 1
    assert findings2[0].line_number == 1


def test_incomplete_trailing_line_is_not_consumed(tmp_path):
    f = tmp_path / "log.txt"
    f.write_text("complete line\nSECRET-VALUE-a")  # no trailing newline
    values = {"x": "SECRET-VALUE-abc123"}
    findings1, watermarks1 = scan_paths([str(f)], values, {})
    assert findings1 == []

    with f.open("a") as fh:
        fh.write("bc123\n")  # completes the line, now matches

    findings2, _ = scan_paths([str(f)], values, watermarks1)
    assert len(findings2) == 1
    assert findings2[0].ref_name == "x"


def test_two_references_sharing_the_same_value_both_reported(tmp_path):
    f = tmp_path / "log.txt"
    f.write_text("shared: SECRET-VALUE-abc123\n")
    values = {"x": "SECRET-VALUE-abc123", "y": "SECRET-VALUE-abc123"}
    findings, _ = scan_paths([str(f)], values, {})
    assert sorted(fnd.ref_name for fnd in findings) == ["x", "y"]


def test_empty_values_never_touches_filesystem(tmp_path):
    f = tmp_path / "log.txt"
    f.write_text("anything at all\n")
    findings, watermarks = scan_paths([str(f)], {}, {})
    assert findings == []
    assert watermarks == {}


def test_nonexistent_glob_is_a_harmless_no_match(tmp_path):
    findings, watermarks = scan_paths(
        [str(tmp_path / "does-not-exist" / "*.log")], {"x": "SECRET-VALUE-abc123"}, {}
    )
    assert findings == []


def test_non_utf8_bytes_are_handled_without_crashing(tmp_path):
    f = tmp_path / "binaryish.log"
    f.write_bytes(b"\xff\xfe garbage \nSECRET-VALUE-abc123\n")
    findings, _ = scan_paths([str(f)], {"x": "SECRET-VALUE-abc123"}, {})
    assert len(findings) == 1
    assert findings[0].line_number == 2


# ---------------------------------------------------------------------------
# secret-boundary-invariant -- structural + behavioral proof
# ---------------------------------------------------------------------------

def test_finding_dataclass_has_no_value_capable_field():
    from portunus.leakscan import Finding

    field_names = set(Finding.__dataclass_fields__.keys())
    assert field_names == {"ref_name", "path", "line_number", "byte_offset"}


def test_leakscan_module_never_calls_print_or_logging():
    import portunus.leakscan as mod

    src = inspect.getsource(mod)
    tree = ast.parse(src)
    for node in ast.walk(tree):
        if isinstance(node, ast.Call) and isinstance(node.func, ast.Name):
            assert node.func.id != "print", "leakscan.py must never print anything -- it may hold decrypted values in memory"


def test_search_error_never_leaks_value_in_exception(tmp_path, monkeypatch):
    f = tmp_path / "log.txt"
    f.write_text("SECRET-VALUE-abc123\n")
    secret_value = "SECRET-VALUE-abc123"

    import portunus.leakscan as mod

    def boom(*_args, **_kwargs):
        raise RuntimeError("boom")

    monkeypatch.setattr(mod, "_compile_pattern", boom)
    with pytest.raises(RuntimeError) as exc_info:
        scan_paths([str(f)], {"x": secret_value}, {})
    assert secret_value not in str(exc_info.value)


# ---------------------------------------------------------------------------
# perf sanity -- catches an accidentally-quadratic implementation
# ---------------------------------------------------------------------------

def test_scan_stays_fast_against_a_multi_mb_file_with_many_patterns(tmp_path):
    f = tmp_path / "big.log"
    line = "just a normal boring log line with nothing interesting in it at all\n"
    with f.open("w") as fh:
        for _ in range(60_000):  # ~4-5 MB
            fh.write(line)
        fh.write("leak here: NEEDLE-VALUE-0099-abcdef\n")

    values = {f"ref{i}": f"NEEDLE-VALUE-{i:04d}-abcdef" for i in range(100)}

    start = time.monotonic()
    findings, _ = scan_paths([str(f)], values, {})
    elapsed = time.monotonic() - start

    assert len(findings) == 1
    assert findings[0].ref_name == "ref99"
    assert elapsed < 5.0, f"scan took {elapsed:.2f}s -- looks quadratic, not a single linear pass"
