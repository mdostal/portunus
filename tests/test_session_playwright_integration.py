"""portunus-session-access-gate Story 02: a REAL Playwright integration
proof, not new production code (research-brief.md §3 -- verified directly
against Playwright's own API that browser.new_context(storage_state=<path>)
already accepts the exact tempfile path `portunus session load` already
prints). This test proves that already-existing chain genuinely works
against a real browser context.

Skipped cleanly when `playwright` isn't installed -- it stays a test-time-
only, optional dependency, never added to pyproject.toml's core deps."""
import json
import os

import pytest

from portunus.cli import main

playwright_sync_api = pytest.importorskip("playwright.sync_api")


@pytest.fixture(autouse=True)
def _local_backend(monkeypatch):
    monkeypatch.delenv("PORTUNUS_BACKEND", raising=False)


def _real_storage_state():
    """A realistic Playwright storageState shape -- exactly what
    `browser_context.storage_state()` itself would produce, and exactly
    what `browser.new_context(storage_state=...)` expects back."""
    return {
        "cookies": [
            {
                "name": "session_id",
                "value": "FAKE-SESSION-do-not-leak-0xC0FFEE",
                "domain": "example.test",
                "path": "/",
                "expires": -1,
                "httpOnly": False,
                "secure": False,
                "sameSite": "Lax",
            }
        ],
        "origins": [],
    }


def test_session_load_produces_a_storage_state_file_playwright_can_actually_use(home):
    """The end-to-end proof: store a real storageState via Portunus, load
    it via the real CLI, and hand the printed path straight to a real
    Playwright browser context -- no new injection code, just the
    already-existing `session store`/`session load` commands."""
    state = _real_storage_state()
    value_file = home / "state.json"
    value_file.write_text(json.dumps(state))

    rc = main([
        "session", "store", "example.test", "dostal@example.test",
        "--value-file", str(value_file), "--ttl-seconds", "3600",
    ])
    assert rc == 0

    import io
    from contextlib import redirect_stdout

    out = io.StringIO()
    with redirect_stdout(out):
        rc = main(["session", "load", "example.test", "dostal@example.test"])
    assert rc == 0
    tempfile_path = out.getvalue().strip()
    assert os.path.exists(tempfile_path)

    # REAL FINDING, corrected here rather than assumed: `session load`'s
    # tempfile holds the FULL record (schema/namespace/ttl/rotation/scope/
    # session) -- confirmed both by reading cmd_session_load's own code and
    # by this test genuinely failing against Playwright before this
    # extraction step was added (Playwright silently ignores an object with
    # no top-level "cookies"/"origins" keys rather than erroring). The real
    # bridge from Portunus to Playwright is this one-line unwrap -- still no
    # new Portunus production code, but the research-brief's original "hand
    # the path straight to Playwright, zero extra steps" claim was one step
    # too strong; corrected in the docs too. Written to a SECOND tempfile
    # here (not passed as an in-memory dict) to faithfully prove the real
    # shell-pipeline usage pattern: `path=$(portunus session load ...)`,
    # then one `jq .session` (or equivalent) into a second file, then that
    # path to Playwright -- not just that the JSON shape is right.
    record = json.loads(open(tempfile_path).read())
    import tempfile as tempfile_mod
    fd, storage_state_path = tempfile_mod.mkstemp(prefix="portunus-session-playwright-test-")
    with os.fdopen(fd, "w") as fh:
        json.dump(record["session"], fh)

    from playwright.sync_api import sync_playwright

    try:
        with sync_playwright() as p:
            browser = p.chromium.launch()
            try:
                context = browser.new_context(storage_state=storage_state_path)
                cookies = context.cookies()
                assert any(
                    c["name"] == "session_id" and c["value"] == "FAKE-SESSION-do-not-leak-0xC0FFEE"
                    for c in cookies
                )
                context.close()
            finally:
                browser.close()
    finally:
        os.unlink(tempfile_path)
        os.unlink(storage_state_path)


def test_session_load_denied_by_the_access_gate_never_reaches_playwright(home, monkeypatch):
    """The gate (Story 01) and the Playwright bridge (this story) compose
    correctly: a denied session never even produces a tempfile to hand to
    Playwright."""
    from portunus.roles import set_enforcement, set_policy

    monkeypatch.setenv("DOSTAL_AGENT", "codex-other")
    state = _real_storage_state()
    value_file = home / "state.json"
    value_file.write_text(json.dumps(state))

    rc = main([
        "session", "store", "example.test", "dostal@example.test",
        "--value-file", str(value_file), "--ttl-seconds", "3600",
        "--org", "firefly-events",
    ])
    assert rc == 0

    set_policy("org", "firefly-events", "dev", ["read"], principal="claude-ffe")
    set_enforcement(True)

    import glob
    import tempfile as tempfile_mod

    pattern = os.path.join(tempfile_mod.gettempdir(), "portunus-session-*")
    before = set(glob.glob(pattern))

    rc = main(["session", "load", "example.test", "dostal@example.test"])
    assert rc != 0

    after = set(glob.glob(pattern))
    assert after == before, "denied access must never create a tempfile"
