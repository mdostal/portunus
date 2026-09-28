"""`portunus health` -- a read-only deep self-check.

Unlike the liveness probes (the UI's `/api/health?shallow=1`, the MCP
`portunus_health(shallow=True)`), this proves the state home is usable:
PORTUNUS_HOME exists and is locked down, the registry parses, the audit
chain verifies, the audit clock agrees with the log, and each configured
backend is reachable where that can be checked without touching a value.

Read-only by construction: never calls paths.home() (which creates and
chmods the directory), never constructs Registry()/AuditChain()/
LocalEncryptedBackend() (their constructors create files), never appends an
audit entry, and never resolves or decrypts a secret value. The one outbound
call is a gcloud `versions describe latest` on one reference -- the same
metadata-only recency probe SyncingBackend uses.

Result shape: {"status": "ok"|"degraded"|"down", "checks": [{name, ok,
detail}]}. A failed check marks the whole result either "down" (Portunus
cannot serve anything: no home, unreadable registry) or "degraded" (it still
serves, but something needs attention).
"""
from __future__ import annotations

import fcntl
import json
import os
import shutil
import stat
import time
from dataclasses import fields
from pathlib import Path
from typing import Callable, Dict, List, Optional

from cryptography.fernet import Fernet

from .audit import verify_entries
from .backend import BackendError, GcloudBackend, VaultBinding
from .paths import home_path
from .registry import Reference

OK, DEGRADED, DOWN = "ok", "degraded", "down"
EXIT_CODES = {OK: 0, DEGRADED: 1, DOWN: 2}

# Files that hold state worth protecting and that Portunus itself always
# writes 0600. Lock files are deliberately absent: they are empty and are
# created with the process umask.
_PRIVATE_FILES = (
    "registry.json", "audit.log", ".clock", "master.key", "vault.enc.json",
    "vault-bindings.json", "gcp-bindings.json", "rotation-bindings.json",
    "roles.json", "roles-enforce.json", "leak-status.json",
)

_STUB_BACKENDS = ("aws", "vault", "infisical", "doppler", "onepassword", "azure")
_INJECTABLE_STATES = ("enabled", "locked")
_AUDIT_LOCK_TIMEOUT = 2.0
_GCLOUD_PROBE_TIMEOUT = 10.0


class _Report:
    def __init__(self) -> None:
        self.checks: List[dict] = []
        self.status = OK

    def add(self, name: str, ok: bool, detail: str, fail_status: str = DEGRADED) -> None:
        self.checks.append({"name": name, "ok": ok, "detail": detail})
        if not ok and (fail_status == DOWN or self.status == OK):
            self.status = fail_status

    def to_dict(self) -> dict:
        return {"status": self.status, "checks": self.checks}


def run_health(runner: Optional[Callable] = None) -> dict:
    """Run every check and return the result dict. `runner` replaces
    subprocess.run for the gcloud probe (tests)."""
    report = _Report()
    base = home_path()

    if not base.is_dir():
        what = "does not exist" if not base.exists() else "is not a directory"
        report.add("home", False, f"{base} {what}", fail_status=DOWN)
        return report.to_dict()
    report.add("home", True, str(base))

    _check_permissions(report, base)
    refs = _check_registry(report, base)
    _check_audit(report, base)
    bindings = _check_bindings(report, base)
    _check_backends(report, base, refs, bindings, runner)
    return report.to_dict()


def exit_code(result: dict) -> int:
    return EXIT_CODES[result["status"]]


# --- checks -----------------------------------------------------------------
def _loose_mode(path: Path) -> Optional[int]:
    """Return the file's permission bits if group/other can access it."""
    mode = stat.S_IMODE(path.stat().st_mode)
    return mode if mode & 0o077 else None


def _check_permissions(report: _Report, base: Path) -> None:
    problems = []
    mode = _loose_mode(base)
    if mode is not None:
        problems.append(f"{base.name}/ is {mode:04o} (want 0700)")
    for name in _PRIVATE_FILES:
        path = base / name
        if path.is_file():
            mode = _loose_mode(path)
            if mode is not None:
                problems.append(f"{name} is {mode:04o} (want 0600)")
    if problems:
        report.add("permissions", False, "; ".join(problems))
    else:
        report.add("permissions", True, "home 0700, state files 0600")


def _check_registry(report: _Report, base: Path) -> List[Reference]:
    path = base / "registry.json"
    if not path.exists():
        report.add("registry", True, "no registry yet (0 references)")
        return []
    try:
        raw = json.loads(path.read_text() or "{}")
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
        report.add("registry", False, f"registry.json does not parse: {exc}", fail_status=DOWN)
        return []
    if not isinstance(raw, dict):
        report.add("registry", False, "registry.json is not a JSON object", fail_status=DOWN)
        return []
    known = {f.name for f in fields(Reference)}
    refs = []
    for key, entry in raw.items():
        if not isinstance(entry, dict) or "name" not in entry or "sm_name" not in entry:
            report.add("registry", False, f"entry {key!r} is missing name/sm_name", fail_status=DOWN)
            return []
        unknown = sorted(set(entry) - known)
        if unknown:
            report.add("registry", False, f"entry {key!r} has unknown fields {unknown}",
                       fail_status=DOWN)
            return []
        refs.append(Reference(**entry))
    report.add("registry", True, f"{len(refs)} references")
    return refs


def _read_audit(base: Path):
    """Return (entries, bad_line_numbers, clock_text) read under the audit
    lock when its lock file already exists, so a concurrent append (which
    ticks the clock before writing its line) can't look like drift. Never
    creates the lock file."""
    lock_path = base / ".clock.lock"
    fh = None
    if lock_path.exists():
        fh = open(lock_path, "r")
        deadline = time.monotonic() + _AUDIT_LOCK_TIMEOUT
        while True:
            try:
                fcntl.flock(fh, fcntl.LOCK_SH | fcntl.LOCK_NB)
                break
            except OSError:
                if time.monotonic() >= deadline:
                    break  # read unlocked rather than hang a health probe
                time.sleep(0.05)
    try:
        entries, bad = [], []
        log = base / "audit.log"
        if log.exists():
            for n, line in enumerate(log.read_text().splitlines(), start=1):
                line = line.strip()
                if not line:
                    continue
                try:
                    entries.append(json.loads(line))
                except json.JSONDecodeError:
                    bad.append(n)
        clock = base / ".clock"
        clock_text = clock.read_text().strip() if clock.exists() else None
        return entries, bad, clock_text
    finally:
        if fh is not None:
            fh.close()  # closing releases the flock


def _check_audit(report: _Report, base: Path) -> None:
    try:
        entries, bad, clock_text = _read_audit(base)
    except OSError as exc:
        report.add("audit_chain", False, f"audit log unreadable: {exc}")
        report.add("audit_clock", False, "audit log unreadable")
        return

    if bad:
        report.add("audit_chain", False, f"unparseable audit lines: {bad[:5]}")
    else:
        try:
            intact = verify_entries(entries)
        except (KeyError, TypeError):
            intact = False
        if intact:
            report.add("audit_chain", True, f"intact ({len(entries)} entries)")
        else:
            report.add("audit_chain", False, f"BROKEN ({len(entries)} entries)")

    seqs = [e.get("seq") for e in entries if isinstance(e, dict)]
    if not all(isinstance(s, int) for s in seqs):
        report.add("audit_clock", False, "audit entries carry a non-integer seq")
        return
    if any(b <= a for a, b in zip(seqs, seqs[1:])):
        report.add("audit_clock", False, "audit seq is not strictly increasing")
        return
    last = seqs[-1] if seqs else 0
    if clock_text is None:
        clock = 0
    else:
        try:
            clock = int(clock_text or "0")
        except ValueError:
            report.add("audit_clock", False, ".clock is not an integer")
            return
    if clock < last:
        report.add("audit_clock", False,
                   f".clock is {clock} but the log reaches seq {last} (clock rolled back)")
    elif clock > last:
        report.add("audit_clock", False,
                   f".clock is {clock} but the log ends at seq {last} "
                   f"({clock - last} events missing, log truncated?)")
    else:
        report.add("audit_clock", True, f"clock {clock} matches last seq")


def _check_bindings(report: _Report, base: Path) -> Dict[str, VaultBinding]:
    """Mirror backend.load_vault_bindings() without its home() fallback."""
    path, legacy = base / "vault-bindings.json", base / "gcp-bindings.json"
    source = path if path.exists() else legacy if legacy.exists() else None
    if source is None:
        project = os.environ.get("PORTUNUS_GCP_PROJECT", "")
        if not project:
            return {}
        audience = os.environ.get("PORTUNUS_GCP_WIF_AUDIENCE", "")
        return {project: VaultBinding(project=project, wif_audience=audience)}
    try:
        raw = json.loads(source.read_text() or "{}")
        if not isinstance(raw, dict):
            raise ValueError("not a JSON object")
        bindings = {
            proj: VaultBinding(
                project=proj,
                wif_audience=cfg.get("wif_audience", ""),
                account=cfg.get("account", ""),
                impersonate_service_account=cfg.get("impersonate_service_account", ""),
                backend=cfg.get("backend", "gcp") if source is path else "gcp",
                sync_mode=cfg.get("sync_mode", "direct") if source is path else "direct",
            )
            for proj, cfg in raw.items()
        }
    except (OSError, ValueError, AttributeError) as exc:
        report.add("bindings", False, f"{source.name} does not parse: {exc}")
        return {}
    report.add("bindings", True, f"{len(bindings)} project bindings")
    return bindings


def _backend_kind(ref: Reference, bindings: Dict[str, VaultBinding], fallback: str) -> str:
    """Same precedence as cli._make_backend_router."""
    if ref.backend:
        return ref.backend
    binding = bindings.get(ref.project)
    return binding.backend if binding is not None else fallback


def _check_backends(report, base, refs, bindings, runner) -> None:
    fallback = os.environ.get("PORTUNUS_BACKEND", "local")
    fallback = "gcp" if fallback == "gcloud" else fallback
    if fallback == "mock":
        report.add("backend:mock", True, "PORTUNUS_BACKEND=mock (dry-run values, nothing to probe)")
        return
    routed: Dict[str, List[Reference]] = {fallback: []}
    for ref in refs:
        routed.setdefault(_backend_kind(ref, bindings, fallback), []).append(ref)
    if "oauth" in routed:
        routed.setdefault("local", [])  # OAuth credentials live in the local vault
    for kind in sorted(routed):
        if kind == "local":
            _check_local(report, base)
        elif kind == "gcp":
            _check_gcp(report, routed[kind], bindings, runner)
        elif kind == "oauth":
            report.add("backend:oauth", True, "served from the local vault")
        elif kind in _STUB_BACKENDS:
            report.add(f"backend:{kind}", False,
                       f"{len(routed[kind])} references route to {kind}, which is a "
                       "fail-closed stub")
        else:
            report.add(f"backend:{kind}", False, f"unknown backend {kind!r}")


def _check_local(report: _Report, base: Path) -> None:
    vault, key = base / "vault.enc.json", base / "master.key"
    if not vault.exists():
        report.add("backend:local", True, "no local vault yet (0 values)")
        return
    try:
        data = json.loads(vault.read_text() or "{}")
        if not isinstance(data, dict):
            raise ValueError("not a JSON object")
    except (OSError, UnicodeDecodeError, ValueError) as exc:
        report.add("backend:local", False, f"vault.enc.json does not parse: {exc}")
        return
    if data and not key.exists():
        report.add("backend:local", False,
                   f"{len(data)} encrypted values but master.key is missing")
        return
    if key.exists():
        try:  # loads the key only; nothing is decrypted
            Fernet(key.read_bytes())
        except (OSError, ValueError) as exc:
            report.add("backend:local", False, f"master.key is not a valid key: {exc}")
            return
    report.add("backend:local", True, f"{len(data)} encrypted values, master key present")


def _check_gcp(report, refs, bindings, runner) -> None:
    if shutil.which("gcloud") is None:
        report.add("backend:gcp", False, "gcloud CLI not found on PATH")
        return
    # A WIF-bound project would mint a credential, and minting appends to
    # the audit log -- not read-only, so those references are never probed.
    candidates = [r for r in refs
                  if not (bindings.get(r.project) and bindings[r.project].wif_audience)]
    candidates.sort(key=lambda r: (r.state not in _INJECTABLE_STATES, r.name))
    if not candidates:
        why = "no gcp references" if not refs else "every gcp reference is WIF-bound"
        report.add("backend:gcp", True, f"gcloud present; probe skipped ({why})")
        return
    ref = candidates[0]
    binding = bindings.get(ref.project)
    backend = GcloudBackend(
        project=os.environ.get("PORTUNUS_GCP_PROJECT", ""),
        timeout=_GCLOUD_PROBE_TIMEOUT,
        runner=runner,
        bindings={ref.project: binding} if binding else None,
    )
    try:
        backend.latest_version(ref.sm_name, project=ref.project)
    except (BackendError, ValueError) as exc:
        report.add("backend:gcp", False, f"latest_version probe on {ref.name} failed: {exc}")
        return
    report.add("backend:gcp", True, f"latest_version probe on {ref.name} ok")
