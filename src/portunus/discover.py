"""ARCA discovery — read-only enumeration of what already exists in a live
GCP Secret Manager project, so it can be registered instead of re-created
blind.

This module never imports GcloudBackend or any SecretBackend implementation
at all -- it is structurally incapable of fetching a value, not merely
disciplined about not doing so. It only ever calls `gcloud secrets list`,
which returns names/labels/create-time, never `versions access`.
"""
from __future__ import annotations

import json
import shutil
import subprocess
from dataclasses import dataclass, field
from typing import Callable, Dict, List, Optional

from .registry import Registry

Runner = Callable[..., object]


class DiscoverError(RuntimeError):
    """Raised when `gcloud secrets list` cannot be completed."""


@dataclass(frozen=True)
class DiscoveredSecret:
    sm_name: str
    labels: Dict[str, str] = field(default_factory=dict)
    create_time: str = ""


@dataclass
class DiscoveryReport:
    already_registered: List[str]
    not_yet_registered: List[DiscoveredSecret]
    registered: List[str]   # populated only when register_discovered() writes
    conflicts: List[str]    # derived-name collisions, skipped, never overwritten


def _default_runner(cmd, capture_output, text, timeout):
    return subprocess.run(cmd, capture_output=capture_output, text=text, timeout=timeout)


def list_gcp_secrets(
    project: str, account: str = "", runner: Optional[Runner] = None, timeout: float = 30.0
) -> List[DiscoveredSecret]:
    """List secret names + labels + create-time for `project`. Never a value.

    `account` selects which already-locally-authenticated gcloud identity to
    use (--account=), independent of gcloud's single mutable "active
    account" -- lets discovery work correctly across multiple GCP accounts
    in the same process. Empty means "use whatever gcloud considers active"
    (unchanged ambient behavior).
    """
    if shutil.which("gcloud") is None:
        raise DiscoverError("gcloud CLI not found on PATH")
    run = runner or _default_runner
    cmd = ["gcloud"]
    if account:
        cmd.append(f"--account={account}")
    cmd.extend(["secrets", "list", f"--project={project}", "--format=json"])
    try:
        proc = run(cmd, capture_output=True, text=True, timeout=timeout)
    except subprocess.TimeoutExpired as exc:
        raise DiscoverError(f"gcloud timeout listing secrets for {project}") from exc
    if proc.returncode != 0:
        raise DiscoverError(
            f"gcloud secrets list failed for {project}: {proc.stderr.strip()[:200]}"
        )
    try:
        raw = json.loads(proc.stdout or "[]")
    except json.JSONDecodeError as exc:
        raise DiscoverError(f"gcloud returned malformed JSON for {project}") from exc

    out: List[DiscoveredSecret] = []
    for entry in raw:
        full_name = entry.get("name", "")
        sm_name = full_name.rsplit("/", 1)[-1] if full_name else ""
        if not sm_name:
            continue
        out.append(DiscoveredSecret(
            sm_name=sm_name,
            labels=dict(entry.get("labels") or {}),
            create_time=entry.get("createTime", ""),
        ))
    return out


def derive_local_name(project: str, sm_name: str) -> str:
    """<project>-<sm_name>, lowercased -- avoids collisions between two GCP
    projects that happen to share a secret name (grill U1)."""
    return f"{project}-{sm_name}".lower()


def diff_against_registry(
    registry: Registry, project: str, discovered: List[DiscoveredSecret]
):
    """Split `discovered` into (already-registered local names, not-yet-registered)."""
    already: List[str] = []
    not_yet: List[DiscoveredSecret] = []
    for d in discovered:
        local_name = derive_local_name(project, d.sm_name)
        existing = registry.get(local_name)
        if existing is not None and existing.sm_name == d.sm_name and existing.project == project:
            already.append(local_name)
        else:
            not_yet.append(d)
    return already, not_yet


def register_discovered(
    registry: Registry, project: str, discovered: List[DiscoveredSecret]
) -> DiscoveryReport:
    """Write not-yet-registered secrets as state=requested placeholders.

    state=requested fails closed automatically (Broker.check_injectable's
    existing allowlist) -- discovery never makes anything injectable. Never
    overwrites an existing reference whose derived name collides but points
    at a different sm_name/project; that's reported as a conflict instead.
    """
    already, not_yet = diff_against_registry(registry, project, discovered)
    registered: List[str] = []
    conflicts: List[str] = []
    for d in not_yet:
        local_name = derive_local_name(project, d.sm_name)
        if registry.get(local_name) is not None:
            conflicts.append(local_name)
            continue
        description = d.labels.get("purpose", "") or d.labels.get("description", "")
        registry.add(
            local_name, d.sm_name, state="requested",
            provider="gcp", project=project, description=description,
        )
        registered.append(local_name)
    return DiscoveryReport(
        already_registered=already, not_yet_registered=not_yet,
        registered=registered, conflicts=conflicts,
    )
