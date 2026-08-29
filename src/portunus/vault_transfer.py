"""portunus vault access export|import|verify (portunus-vault-transfer).

Lets a SECOND Portunus instance gain working access to some or all of what
a FIRST instance's vault already exposes -- without ever moving a secret
value. Distinct from `portunus vault export/import` (backup.py -- a
full-vault, passphrase-locked, local-VALUE backup/restore) and distinct
from bidirectional sync (explicitly out of scope, portunus-vault-backup's
own design-discussion §4). For the majority of real references
(GCP/AWS-backed), the value lives in the cloud provider, not locally -- only
the registry pointer + vault-bindings config ever need to move.

Zero secret-boundary surface, by construction: this module has no
legitimate reason to import LocalEncryptedBackend/GcloudBackend/
SecretBackend/Broker -- it only ever reads/writes registry and bindings
metadata (verified structurally in tests/test_vault_transfer_export.py).
"""
from __future__ import annotations

import json
import os
from dataclasses import asdict
from pathlib import Path
from typing import Dict, Optional

from .backend import VaultBinding
from .registry import Reference, Registry
from .rotation import RotationBinding

# PORTUNUS_BACKEND spells the GCP choice "gcloud" (cli.py::_build), but
# every OTHER kind string in this codebase -- ref.backend, VaultBinding.
# backend, cli.py::_make_backend_router's own _for_kind() branches --
# spells it "gcp". resolved_backend_kind() must report the SAME vocabulary
# those other fields use, not the env var's own spelling, or a consumer
# comparing this field against ref.backend/VaultBinding.backend would see
# a false mismatch for the exact common case (no override, no binding,
# relying on the global GCP default).
_GLOBAL_FALLBACK_KIND_ALIASES = {"gcloud": "gcp"}

DEFAULT_BUNDLE_FILENAME = "portunus-vault-access.json"


def _global_fallback_kind() -> str:
    raw = os.environ.get("PORTUNUS_BACKEND", "local")
    return _GLOBAL_FALLBACK_KIND_ALIASES.get(raw, raw)


def resolved_backend_kind(ref: Reference, vault_bindings: Dict[str, VaultBinding]) -> str:
    """Mirrors cli.py::_make_backend_router's own 3-level precedence
    (ref.backend override -> project's VaultBinding.backend -> the global
    PORTUNUS_BACKEND fallback) -- string-only, computed on the SOURCE
    instance at export time, since that's the only place this precedence
    is actually knowable (the target instance doesn't share the source's
    global fallback). A cached-mode GCP binding still reports "gcp" --
    SyncingBackend is fetch-strategy detail, not a different backend kind
    for the "is this local" question this field exists to answer."""
    if ref.backend:
        return ref.backend
    binding = vault_bindings.get(ref.project)
    if binding is not None:
        return binding.backend
    return _global_fallback_kind()


def _matches_all(ref: Reference, project: str, org: str, tags: Dict[str, str]) -> bool:
    if project and ref.project != project:
        return False
    if org and ref.org != org:
        return False
    for key, value in tags.items():
        if not ref.matches_tag(key, value):
            return False
    return True


def _parse_tags(raw: str) -> Dict[str, str]:
    tags: Dict[str, str] = {}
    for pair in raw.split(","):
        pair = pair.strip()
        if not pair:
            continue
        if "=" not in pair:
            raise ValueError(f"invalid tags entry (want k=v): {pair!r}")
        k, v = pair.split("=", 1)
        tags[k.strip()] = v.strip()
    return tags


def build_bundle(
    registry: Registry,
    vault_bindings: Dict[str, VaultBinding],
    rotation_bindings: Dict[str, RotationBinding],
    project: str = "",
    org: str = "",
    tags: str = "",
) -> dict:
    """No filter = every reference, matching `reg json`'s own
    no-filter-means-everything convention. Never passphrase-locked --
    structurally cannot contain a value: only Reference/VaultBinding/
    RotationBinding fields, all non-secret by their own docstrings."""
    parsed_tags = _parse_tags(tags)
    matched = [r for r in registry if _matches_all(r, project, org, parsed_tags)]

    ref_entries = []
    included_projects = set()
    included_providers = set()
    for ref in matched:
        entry = asdict(ref)
        entry["resolved_backend"] = resolved_backend_kind(ref, vault_bindings)
        ref_entries.append(entry)
        if ref.project:
            included_projects.add(ref.project)
        if ref.provider:
            included_providers.add(ref.provider)

    bundle_vault_bindings = {
        proj: asdict(vault_bindings[proj]) for proj in included_projects if proj in vault_bindings
    }
    bundle_rotation_bindings = {
        provider: asdict(rotation_bindings[provider])
        for provider in included_providers
        if provider in rotation_bindings
    }

    return {
        "format_version": 1,
        "references": ref_entries,
        "vault_bindings": bundle_vault_bindings,
        "rotation_bindings": bundle_rotation_bindings,
    }


def write_bundle(bundle: dict, out: Optional[str] = None) -> Path:
    path = Path(out) if out else Path.cwd() / DEFAULT_BUNDLE_FILENAME
    path.write_text(json.dumps(bundle, indent=2))
    return path
