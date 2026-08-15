"""Coordinated multi-file snapshot -- the foundation for `portunus vault
export`/`import` (portunus-vault-backup story 03). This module (story 02) is
infrastructure only: a primitive that reads the vault's critical-state
surface as one consistent instant, nothing else.

Acquires every lock protecting that surface -- registry.lock,
vault.enc.lock (guards both master.key and vault.enc.json together, the
same pairing LocalEncryptedBackend itself protects as one file's worth of
state), and vault-bindings.lock -- plus audit.log's own .clock.lock, in a
FIXED order (sorted by lock filename, never any other order, anywhere this
primitive is used) to prevent a lock-ordering deadlock against any future
caller that needs more than one of these locks at once. Reads every present
target file's raw bytes while holding every lock simultaneously, then
releases. The result reflects one consistent instant -- never independent
reads straddling a concurrent writer's mutation.

gcp-bindings.json (legacy) and rotation-bindings.json are read WITHOUT a
lock: neither has a dedicated writer-side lock today (this epic adds one for
vault-bindings.json only, per its own confirmed scope -- see
design-discussion.md §3), so there is no lock to acquire for either. Reading
either unlocked is the same posture this codebase already accepts for every
other unlocked read: os.replace()'s atomicity means a reader never observes
a torn write, only possibly-stale content, which is what a legacy/optional
file already tolerates.
"""
from __future__ import annotations

import base64
import json
import os
from contextlib import ExitStack, contextmanager
from pathlib import Path
from typing import Dict, List, Optional

from cryptography.fernet import Fernet, InvalidToken
from cryptography.hazmat.primitives import hashes
from cryptography.hazmat.primitives.kdf.pbkdf2 import PBKDF2HMAC

from .filelock import flock_path
from .paths import home

# OWASP-recommended minimum for PBKDF2-HMAC-SHA256 (2023 cheat sheet) --
# deliberately not a home-rolled KDF or iteration count (design-discussion.md
# §2). The export archive's own passphrase-derived key is the ONLY thing
# protecting a bundle that otherwise contains the whole vault (encrypted at
# rest, but master.key + vault.enc.json TOGETHER are enough to decrypt every
# stored value) -- weak KDF parameters here would undermine that boundary.
PBKDF2_ITERATIONS = 600_000
ARCHIVE_FORMAT_VERSION = 1


class ExportError(RuntimeError):
    """export/import failed -- see message. Never carries a secret value or
    a passphrase; only file/archive-shape errors and InvalidToken's own
    generic "wrong key" signal."""


def _derive_key(passphrase: str, salt: bytes, iterations: int = PBKDF2_ITERATIONS) -> bytes:
    kdf = PBKDF2HMAC(algorithm=hashes.SHA256(), length=32, salt=salt, iterations=iterations)
    return base64.urlsafe_b64encode(kdf.derive(passphrase.encode("utf-8")))

# filename -> lock filename, for every file this snapshot serializes against
# before reading. Dict iteration order here is irrelevant to correctness --
# snapshot() derives the one fixed acquisition order itself (sorted by lock
# filename), so there is exactly one order, defined in exactly one place.
LOCKED_FILES: Dict[str, str] = {
    "registry.json": "registry.lock",
    "master.key": "vault.enc.lock",
    "vault.enc.json": "vault.enc.lock",
    "vault-bindings.json": "vault-bindings.lock",
    "audit.log": ".clock.lock",
    # The monotonic seq counter AuditChain._tick() drives append()'s `seq`
    # from -- MUST travel with audit.log, not just alongside it: restoring
    # audit.log without also restoring .clock would reset the counter to 0,
    # so the next append() after an import could re-mint a seq that already
    # exists in the restored chain, breaking append()'s own hash-chain
    # invariant. Same lock as audit.log (both are protected together by
    # AuditChain._locked(), which is keyed off .clock's own lock file).
    ".clock": ".clock.lock",
}

# Read as-is, without a lock -- see module docstring.
UNLOCKED_FILES: List[str] = ["gcp-bindings.json", "rotation-bindings.json"]


@contextmanager
def _acquire_all(lock_paths: List[Path]):
    """Acquire every lock in `lock_paths` (already caller-sorted into the
    one fixed order), all-or-nothing, via a single ExitStack -- if any
    acquisition times out, every lock already held is released before the
    exception propagates."""
    with ExitStack() as stack:
        for lock_path in lock_paths:
            stack.enter_context(flock_path(lock_path))
        yield


def snapshot(base: Optional[Path] = None) -> Dict[str, bytes]:
    """Return {filename: raw_bytes} for every present critical-state file,
    taken as one consistent instant under every relevant lock held
    simultaneously. Missing optional files (legacy gcp-bindings.json,
    rotation-bindings.json, or an as-yet-unused master.key/vault.enc.json on
    a fresh vault) are simply omitted -- never an error."""
    base = Path(base) if base else home()
    lock_filenames = sorted(set(LOCKED_FILES.values()))
    lock_paths = [base / name for name in lock_filenames]

    result: Dict[str, bytes] = {}
    with _acquire_all(lock_paths):
        for filename in LOCKED_FILES:
            path = base / filename
            if path.exists():
                result[filename] = path.read_bytes()
    for filename in UNLOCKED_FILES:
        path = base / filename
        if path.exists():
            result[filename] = path.read_bytes()
    return result


def export_archive(out_path: Path, passphrase: str, base: Optional[Path] = None) -> Path:
    """Coordinated snapshot (snapshot() above) -> a passphrase-locked
    archive. The archive at rest carries no usable key material -- only
    PBKDF2 parameters (a random salt, the iteration count; neither is
    secret) and Fernet ciphertext. Never bundles master.key un-re-encrypted
    (design-discussion.md §2): master.key alone would let anyone holding
    the archive decrypt every stored value, so the whole bundle -- including
    master.key's own bytes -- is encrypted again under a key derived from
    `passphrase`, which the caller is responsible for sourcing safely (CLI:
    PORTUNUS_EXPORT_PASSPHRASE or an interactive prompt, never an inline
    flag)."""
    if not passphrase:
        raise ExportError("a passphrase is required -- refusing to export unencrypted")
    files = snapshot(base)
    bundle = {
        "format_version": ARCHIVE_FORMAT_VERSION,
        "files": {name: base64.b64encode(data).decode("ascii") for name, data in files.items()},
    }
    salt = os.urandom(16)
    key = _derive_key(passphrase, salt)
    ciphertext = Fernet(key).encrypt(json.dumps(bundle).encode("utf-8"))
    archive = {
        "portunus_vault_export": ARCHIVE_FORMAT_VERSION,
        "kdf": "pbkdf2-sha256",
        "iterations": PBKDF2_ITERATIONS,
        "salt": base64.b64encode(salt).decode("ascii"),
        "ciphertext": ciphertext.decode("ascii"),
    }
    out_path = Path(out_path)
    out_path.parent.mkdir(parents=True, exist_ok=True)
    out_path.write_text(json.dumps(archive))
    os.chmod(out_path, 0o600)
    return out_path


def _target_has_existing_state(base: Path) -> bool:
    return any((base / name).exists() for name in list(LOCKED_FILES) + UNLOCKED_FILES)


def import_archive(
    archive_path: Path, passphrase: str, base: Optional[Path] = None, force: bool = False,
) -> List[str]:
    """Reverse of export_archive(): decrypt (fails closed, clear error, on
    a wrong passphrase -- mirrors LocalEncryptedBackend.access()'s own
    InvalidToken handling) and write every bundled file into `base`
    (default PORTUNUS_HOME). Refuses a target that already has any tracked
    critical-state file present unless `force` is set -- a full replace,
    never a merge (design-discussion.md §5). Writes happen under the same
    fixed lock order snapshot()/export_archive() use, so a concurrent
    reader never observes a partially-imported vault."""
    if not passphrase:
        raise ExportError("a passphrase is required to decrypt this archive")
    archive_path = Path(archive_path)
    try:
        archive = json.loads(archive_path.read_text())
    except (OSError, json.JSONDecodeError) as exc:
        raise ExportError(f"cannot read archive: {exc}") from exc
    try:
        salt = base64.b64decode(archive["salt"])
        iterations = int(archive["iterations"])
        ciphertext = archive["ciphertext"].encode("ascii")
    except (KeyError, ValueError) as exc:
        raise ExportError(f"malformed archive: {exc}") from exc

    key = _derive_key(passphrase, salt, iterations)
    try:
        plaintext = Fernet(key).decrypt(ciphertext)
    except InvalidToken:
        raise ExportError("wrong passphrase, or a corrupted archive") from None

    bundle = json.loads(plaintext)
    files = bundle.get("files", {})

    base = Path(base) if base else home()
    if not force and _target_has_existing_state(base):
        raise ExportError(
            f"{base} already has vault state present -- refusing to import without --force "
            "(a full replace, not a merge)"
        )

    base.mkdir(parents=True, exist_ok=True)
    lock_filenames = sorted(set(LOCKED_FILES.values()))
    lock_paths = [base / name for name in lock_filenames]
    written: List[str] = []
    with _acquire_all(lock_paths):
        for filename, b64_data in files.items():
            target = base / filename
            target.write_bytes(base64.b64decode(b64_data))
            os.chmod(target, 0o600)
            written.append(filename)
    return sorted(written)
