"""Shared state-home resolution.

All Portunus state (registry, audit log, approval tokens, monotonic clock)
lives under one 0700 home directory so it is easy to lock down and easy to
wipe. Order of precedence:

    PORTUNUS_HOME  ->  DOSTAL_SECRETS_HOME  ->  ~/.portunus

DOSTAL_SECRETS_HOME is honored for drop-in compatibility with the original
``bin/secrets`` broker.
"""
from __future__ import annotations

import os
from pathlib import Path


def home() -> Path:
    """Return the Portunus state home, creating it 0700 if needed."""
    raw = os.environ.get("PORTUNUS_HOME") or os.environ.get("DOSTAL_SECRETS_HOME")
    path = Path(raw).expanduser() if raw else Path.home() / ".portunus"
    is_new = not path.exists()
    path.mkdir(parents=True, exist_ok=True)
    try:
        path.chmod(0o700)
    except OSError:
        pass
    if is_new:
        # portunus-petitio-rbac Story 04: a genuinely brand-new PORTUNUS_HOME
        # starts with enforcement on. This is the ONE place in the whole
        # codebase that can reliably tell "this directory never existed
        # before" apart from "an existing vault that just hasn't been
        # touched yet" -- registry.json/roles.json aren't reliable signals,
        # since by the time check_injectable() ever runs, a real registry
        # entry (and therefore registry.json) already exists. Stamped
        # exactly once; a later `roles enforce off` sticks, since this
        # branch only fires when the directory didn't exist a moment ago.
        # Deferred import to avoid a cycle -- roles.py itself imports
        # home() from this module.
        from .roles import set_enforcement
        set_enforcement(True, path=path / "roles-enforce.json")
    return path
