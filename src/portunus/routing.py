"""Routing consistency: does each reference resolve from where its value is?

A reference's backend is chosen by `cli._make_backend_router`: an explicit
`ref.backend` wins, otherwise the project's VaultBinding, otherwise the
global fallback (local). The write paths (`drop`, `drop-bulk`, `prompt`,
`portunus_prompt`, rotations) always write to the LOCAL vault, under the
plain `sm_name`. A reference with `backend=""` in a project bound to a
remote store (GCP, ...) therefore resolves from a store its value was never
written to (#242, #246).

Key scheme in the local vault, one meaning per shape:

- plain `sm_name`          -- a value that LIVES in the local vault (written
                              by drop/prompt/rotation). Authoritative.
- `"<project>:<sm_name>"`  -- SyncingBackend's cache of a REMOTE value
                              (gcp + sync_mode=cached). Never authoritative;
                              refreshed from the remote.

Everything here is metadata only: it reads the KEY NAMES of
vault.enc.json, never decrypts a value, never touches the network.
"""
from __future__ import annotations

import json
from dataclasses import dataclass, asdict
from pathlib import Path
from typing import Dict, Iterable, List, Optional, Set

# Backends that hold values remotely and are addressed by the same plain
# `sm_name` a drop writes locally -- the only kinds a local value can be
# stranded behind. oauth/chrome/bitwarden/keychain use their own sm_name schemes and
# never hold drop-written values.
REMOTE_STORE_KINDS = ("gcp", "aws", "vault", "infisical", "doppler", "onepassword", "azure")

FIX_COMMAND = "portunus doctor --fix-routing"


@dataclass
class Mismatch:
    name: str
    sm_name: str
    project: str
    state: str
    routes_to: str        # the backend kind the router sends it to
    sync_mode: str        # the binding's sync_mode ("" when routed explicitly)
    has_cache_copy: bool  # a "<project>:<sm_name>" cache entry also exists

    def to_dict(self) -> dict:
        return asdict(self)


def effective_kind(ref, bindings: Dict[str, object], fallback: str = "local") -> str:
    """Same precedence as cli._make_backend_router (and health._backend_kind)."""
    if ref.backend:
        return ref.backend
    binding = bindings.get(ref.project)
    return binding.backend if binding is not None else fallback


def local_vault_keys(base: Path) -> Set[str]:
    """Key names in `base`/vault.enc.json -- never a value. Empty when the
    vault doesn't exist or doesn't parse (health reports that separately)."""
    path = Path(base) / "vault.enc.json"
    try:
        data = json.loads(path.read_text() or "{}")
    except (OSError, UnicodeDecodeError, ValueError):
        return set()
    return set(data) if isinstance(data, dict) else set()


def find_mismatches(
    refs: Iterable, bindings: Dict[str, object], local_keys: Set[str], fallback: str = "local",
) -> List[Mismatch]:
    """References that follow their project's binding (`backend=""`) to a
    remote store while their value sits in the local vault under the plain
    `sm_name`. An explicit `ref.backend` is the operator's own statement of
    where the value lives and is not reported."""
    out: List[Mismatch] = []
    for ref in refs:
        if ref.backend or not ref.sm_name or ref.sm_name not in local_keys:
            continue
        kind = effective_kind(ref, bindings, fallback)
        if kind not in REMOTE_STORE_KINDS:
            continue
        binding = bindings.get(ref.project)
        out.append(Mismatch(
            name=ref.name, sm_name=ref.sm_name, project=ref.project, state=ref.state,
            routes_to=kind, sync_mode=getattr(binding, "sync_mode", "") if binding else "",
            has_cache_copy=f"{ref.project}:{ref.sm_name}" in local_keys,
        ))
    return sorted(out, key=lambda m: m.name)


def rerouted_by_binding(
    refs: Iterable, project: str, new_backend: str, local_keys: Set[str],
    previous_backend: Optional[str] = None,
) -> List:
    """References a `bindings set <project> --backend <new_backend>` would
    strand: they follow the binding, their value is local, and the change
    sends them to a remote store they were not routed to before."""
    if new_backend not in REMOTE_STORE_KINDS or previous_backend == new_backend:
        return []
    return sorted(
        (r for r in refs
         if r.project == project and not r.backend and r.sm_name in local_keys),
        key=lambda r: r.name,
    )


def summarize(mismatches: List[Mismatch], limit: int = 5) -> str:
    """One line for health / vault status."""
    names = ", ".join(m.name for m in mismatches[:limit])
    more = f" (+{len(mismatches) - limit} more)" if len(mismatches) > limit else ""
    return (f"{len(mismatches)} reference(s) route to a remote backend but their value is "
            f"in the local vault: {names}{more} -- run `{FIX_COMMAND}` (dry run) to repair")
