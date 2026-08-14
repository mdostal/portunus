"""Rotation provenance -- which provider a reference came from, what
account/context a rotation would run against, and whether Portunus has a
real adapter for it yet.

Mirrors ARCA's own real/stub posture (backend.py) applied to a genuinely
different kind of provider integration: not "where does the value live"
but "who would rotate it, and how." `RotationBinding` is the direct analog
of `VaultBinding` -- keyed by provider (not per-reference, same per-project
reasoning), persisted as PORTUNUS_HOME/rotation-bindings.json, 0600,
atomic-replace. Every `RotationAdapter` here is a stub -- `.rotate()`
unconditionally raises, matching every ARCA stub backend's own restraint.
No real provider API is ever called from this module.

A future real adapter (Vercel is the confirmed priority target) would
authenticate using its OWN admin credential -- itself just another
Portunus-managed Reference, resolved through Resolver.resolve_call() the
same boundary-only way every other value in this codebase is. Portunus
rotating secrets using a Portunus-managed secret, never a special-cased
credential path. See docs/architecture.md for the worked example.
"""
from __future__ import annotations

import json
import os
from dataclasses import dataclass
from pathlib import Path
from typing import Dict, Optional

from .paths import home

_ADAPTER_REQUEST_URL = "https://github.com/mdostal/portunus/issues/new?template=adapter-request.yaml"


class RotationAdapterError(RuntimeError):
    """Raised when a rotation adapter cannot rotate a credential (always,
    for a stub -- no real adapter exists yet)."""


@dataclass(frozen=True)
class RotationBinding:
    """Which provider, what account/context, and whether a real adapter
    exists for it yet. `status` mirrors ARCA's own "real" | "stub"
    language. `account` is a free-text, provider-specific context hint
    (e.g. a Vercel team slug, a GitHub org) -- never a credential."""

    provider: str
    status: str = "stub"
    account: str = ""


def _rotation_bindings_path(path: Optional[Path] = None) -> Path:
    return path or (home() / "rotation-bindings.json")


def load_rotation_bindings(path: Optional[Path] = None) -> Dict[str, RotationBinding]:
    """Load PORTUNUS_HOME/rotation-bindings.json (provider -> RotationBinding).
    Missing file means no bindings configured yet -- returns {}, matching
    load_vault_bindings' empty-state handling."""
    bindings_path = _rotation_bindings_path(path)
    if not bindings_path.exists():
        return {}
    raw = json.loads(bindings_path.read_text() or "{}")
    return {
        provider: RotationBinding(
            provider=provider,
            status=cfg.get("status", "stub"),
            account=cfg.get("account", ""),
        )
        for provider, cfg in raw.items()
    }


def save_rotation_bindings(
    bindings: Dict[str, RotationBinding], path: Optional[Path] = None
) -> None:
    """Persist provider rotation bindings, 0600 on disk, atomic replace --
    same idiom save_vault_bindings uses."""
    bindings_path = _rotation_bindings_path(path)
    bindings_path.parent.mkdir(parents=True, exist_ok=True)
    raw = {
        provider: {"status": b.status, "account": b.account}
        for provider, b in bindings.items()
    }
    tmp = bindings_path.with_suffix(".json.tmp")
    tmp.write_text(json.dumps(raw, indent=2))
    os.chmod(tmp, 0o600)
    os.replace(tmp, bindings_path)
    os.chmod(bindings_path, 0o600)


class VercelRotationAdapter:
    """Vercel -- STUB. No real calls.

    Confirmed by the user as the priority target for the first REAL
    rotation adapter, but not built real this epic ("not going to care too
    much for first version"). Once real, `.rotate()` would resolve its own
    admin token via `resolver.resolve_call("{{secret:portunus-admin-vercel-
    token}}", ...)` -- never hardcoded here, never handled outside the
    normal boundary-only sinks.
    """

    def rotate(self, ref, resolver=None) -> None:
        raise RotationAdapterError(
            f"Vercel rotation is not yet implemented -- request it: {_ADAPTER_REQUEST_URL}"
        )


class GitHubRotationAdapter:
    """GitHub -- STUB. No real calls."""

    def rotate(self, ref, resolver=None) -> None:
        raise RotationAdapterError(
            f"GitHub rotation is not yet implemented -- request it: {_ADAPTER_REQUEST_URL}"
        )


class StripeRotationAdapter:
    """Stripe -- STUB. No real calls."""

    def rotate(self, ref, resolver=None) -> None:
        raise RotationAdapterError(
            f"Stripe rotation is not yet implemented -- request it: {_ADAPTER_REQUEST_URL}"
        )


_ADAPTERS = {
    "vercel": VercelRotationAdapter,
    "github": GitHubRotationAdapter,
    "stripe": StripeRotationAdapter,
}


def rotation_adapter_for(provider: str):
    """Resolve a provider name to its RotationAdapter instance, or None if
    no adapter (real or stub) is registered for it yet."""
    adapter_cls = _ADAPTERS.get(provider)
    return adapter_cls() if adapter_cls else None
