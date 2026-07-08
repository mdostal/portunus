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
    path.mkdir(parents=True, exist_ok=True)
    try:
        path.chmod(0o700)
    except OSError:
        pass
    return path
