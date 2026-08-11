"""Reference registry — name -> Secret Manager location, NEVER the value.

A ``Reference`` records where a secret lives (its Secret Manager name/path),
its scope/kind, its lifecycle state, and whether access is approval-gated. It
deliberately has no field for the secret value: the registry is safe to read,
copy, and inspect. The value is only ever fetched, at the call boundary, by
the resolver.
"""
from __future__ import annotations

import json
import os
from dataclasses import dataclass, asdict
from pathlib import Path
from typing import Dict, Iterator, Optional

from .paths import home

# Lifecycle states, mirroring bin/secrets. "enabled"/"locked" are injectable;
# "dropped"/"revoked" fail closed.
VALID_STATES = ("enabled", "locked", "dropped", "revoked")


@dataclass
class Reference:
    """A pointer to a secret. Holds location + policy metadata, never a value."""

    name: str            # the reference / placeholder key, e.g. "shared-anthropic"
    sm_name: str         # Secret Manager secret name, e.g. "dostal-shared-anthropic"
    scope: str = ""      # "shared" or a client slug
    kind: str = ""       # gemini | anthropic | linear | slack | ...
    state: str = "enabled"
    approval: str = ""   # "required" if access is gated
    sm_path: str = ""    # projects/<p>/secrets/<sm_name> (informational)

    def to_dict(self) -> dict:
        return asdict(self)


class Registry:
    """A JSON-backed map of reference name -> Reference. 0600 on disk."""

    def __init__(self, path: Optional[Path] = None):
        self.path = Path(path) if path else home() / "registry.json"
        self._data: Dict[str, Reference] = {}
        self._load()

    # --- persistence -----------------------------------------------------
    def _load(self) -> None:
        if not self.path.exists():
            self._data = {}
            return
        try:
            raw = json.loads(self.path.read_text() or "{}")
        except (json.JSONDecodeError, OSError):
            raw = {}
        self._data = {k: Reference(**v) for k, v in raw.items()}

    def _flush(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        tmp = self.path.with_suffix(".json.tmp")
        tmp.write_text(json.dumps({k: v.to_dict() for k, v in self._data.items()}, indent=2))
        os.chmod(tmp, 0o600)
        os.replace(tmp, self.path)
        os.chmod(self.path, 0o600)

    # --- mutation --------------------------------------------------------
    def add(
        self,
        name: str,
        sm_name: str,
        scope: str = "",
        kind: str = "",
        state: str = "enabled",
        approval: str = "",
        project: str = "",
    ) -> Reference:
        """Register (or overwrite) a reference. Value is never accepted here."""
        if state not in VALID_STATES:
            raise ValueError(f"invalid state: {state!r} (want one of {VALID_STATES})")
        sm_path = f"projects/{project}/secrets/{sm_name}" if project else ""
        ref = Reference(
            name=name, sm_name=sm_name, scope=scope, kind=kind,
            state=state, approval=approval, sm_path=sm_path,
        )
        self._data[name] = ref
        self._flush()
        return ref

    def remove(self, name: str) -> bool:
        existed = self._data.pop(name, None) is not None
        if existed:
            self._flush()
        return existed

    def set_state(self, name: str, state: str) -> Reference:
        if state not in VALID_STATES:
            raise ValueError(f"invalid state: {state!r}")
        ref = self.require(name)
        ref.state = state
        self._flush()
        return ref

    def set_approval(self, name: str, required: bool) -> Reference:
        ref = self.require(name)
        ref.approval = "required" if required else ""
        self._flush()
        return ref

    # --- lookup ----------------------------------------------------------
    def get(self, name: str) -> Optional[Reference]:
        return self._data.get(name)

    def require(self, name: str) -> Reference:
        ref = self._data.get(name)
        if ref is None:
            raise KeyError(name)
        return ref

    def __contains__(self, name: object) -> bool:
        return name in self._data

    def __iter__(self) -> Iterator[Reference]:
        return iter(self._data.values())

    def __len__(self) -> int:
        return len(self._data)
