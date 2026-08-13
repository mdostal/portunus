"""Backend-side proof that the UI's write route cannot bypass Broker.check_
injectable (story 06). The UI never reimplements gating logic in TypeScript
-- its Next.js API routes shell out to the exact same `portunus` console
script exercised here via subprocess, so this test IS the gate proof for the
UI, not a UI-level trust assumption. Also proves the exact stdin-piping
invocation shape the add-secret route uses never leaks the value via
stdout/stderr/argv."""
import json
import os
import subprocess
import sys

import pytest

SECRET = "s3kr3t-do-not-leak-0xCAFE"


def _run(args, home, env_extra=None, input_text=None):
    env = dict(os.environ)
    env["PORTUNUS_HOME"] = str(home)
    env["USER"] = "tester"
    env.pop("PORTUNUS_BACKEND", None)
    env.pop("DOSTAL_AGENT", None)
    env.pop("DOSTAL_TASK", None)
    if env_extra:
        env.update(env_extra)
    return subprocess.run(
        ["portunus", *args], env=env, input=input_text,
        capture_output=True, text=True, timeout=10,
    )


def test_ui_add_route_shape_lands_dropped_not_injectable(home):
    """Exactly the invocation shape the add-secret form's backend route uses:
    value piped via stdin, never an argv flag. Confirms the write is
    gated -- freshly dropped secrets fail closed (state=dropped) until a
    separate, explicit enable step, same as the CLI's own contract."""
    proc = _run(
        ["drop", "ui-added", "sm-ui-added", "--stdin"],
        home, input_text=SECRET + "\n",
    )
    assert proc.returncode == 0
    assert SECRET not in proc.stdout
    assert SECRET not in proc.stderr

    # Not yet injectable -- proves the UI's write route can't skip the gate.
    resolve = _run(["resolve", "{{secret:ui-added}}"], home)
    assert resolve.returncode != 0
    assert "dropped" in resolve.stderr.lower()
    assert SECRET not in resolve.stdout
    assert SECRET not in resolve.stderr


def test_ui_cannot_grant_injectability_via_reg_add_alone(home):
    """`reg add` (metadata only, no gate) must never make a reference
    injectable -- only a real drop (or an explicit state change on an
    already-dropped-with-a-value reference) can. If the UI's route ever
    used `reg add` for the "add secret" flow instead of `drop`, this test
    would catch it: reg add never stores a value, so resolve must fail for
    a completely different reason (unknown backend value), proving there is
    no shortcut around Broker + a real stored value."""
    add = _run(["reg", "add", "meta-only", "sm-meta-only"], home)
    assert add.returncode == 0

    resolve = _run(["resolve", "{{secret:meta-only}}"], home)
    assert resolve.returncode != 0
    assert SECRET not in resolve.stdout
    assert SECRET not in resolve.stderr


def test_ui_registry_json_route_never_contains_a_value(home):
    """The Console/Vault Map data source (`portunus reg json`) is metadata
    only -- proves the read-side route can't leak a value either."""
    _run(["drop", "x", "sm-x", "--stdin"], home, input_text=SECRET + "\n")
    proc = _run(["reg", "json"], home)
    assert proc.returncode == 0
    data = json.loads(proc.stdout)
    assert "x" in data
    assert SECRET not in proc.stdout


def test_ui_audit_json_route_never_contains_a_value(home):
    """The detail-drawer data source (`portunus audit --json`) is metadata
    only."""
    _run(["drop", "y", "sm-y", "--stdin"], home, input_text=SECRET + "\n")
    proc = _run(["audit", "--json", "--secret", "sm-y"], home)
    assert proc.returncode == 0
    entries = json.loads(proc.stdout)
    assert entries
    assert SECRET not in proc.stdout
