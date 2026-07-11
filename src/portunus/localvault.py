"""Local encrypted-at-rest secret vault — the LOCAL tier of Portunus.

For machines without WIF + a cloud Secret Manager: a roll-your-own, locally
locked-down vault. Secrets are encrypted on disk (never plaintext) under a
256-bit master key that lives in the macOS Keychain (or, on non-mac hosts, a
0600 key file). Lookup happens only through this vault — it implements the
same ``SecretBackend`` protocol as ``GcloudBackend``, so the broker, the
resolver, and every lifecycle/audit guarantee apply unchanged.

Crypto (stdlib-only, standard primitives — no new dependencies):

  * master key: 32 random bytes (``secrets.token_bytes``)
  * per-version keys: HMAC-SHA256(master, label || nonce)  (key separation)
  * cipher: CTR-mode keystream from HMAC-SHA256(enc_key, nonce || counter)
  * integrity: encrypt-then-MAC — HMAC-SHA256(mac_key, len(aad)||aad||ct),
    verified with ``hmac.compare_digest`` before any decryption
  * AAD binds each ciphertext to its secret name + version number, so a blob
    cannot be swapped between secrets or replayed as a different version

The vault file holds only ciphertext + metadata. The plaintext exists in
process memory during ``access()`` and flows only to the resolver's boundary
sinks. Nothing here ever logs, prints, or returns a key or value except
``access()`` itself, which only the resolver/broker path should call.
"""
from __future__ import annotations

import base64
import hashlib
import hmac
import json
import os
import re
import secrets as _secrets
import shutil
import subprocess
import sys
import time
from pathlib import Path
from typing import Dict, List, Optional

from .backend import BackendError
from .paths import home

ALG = "hmac-sha256-ctr.etm.v1"
KEY_BYTES = 32
_NAME_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,127}$")


class LocalKeyError(RuntimeError):
    """The master key could not be loaded or created."""


class VaultIntegrityError(RuntimeError):
    """A vault blob failed authentication (tampered, or wrong master key)."""


# --- master-key providers --------------------------------------------------
class KeychainKeyProvider:
    """Master key in the macOS login Keychain (service ``portunus-local-vault``).

    The key is created once and read back with ``security``. On create, the
    key hex is fed to ``security -i`` over *stdin*, never argv, so it is not
    visible in ``ps`` or shell history.
    """

    SERVICE = "portunus-local-vault"

    def __init__(self, account: str = "", runner=None):
        self.account = account or os.environ.get("PORTUNUS_KEYCHAIN_ACCOUNT", "master")
        self.runner = runner or subprocess.run

    def key(self) -> bytes:
        existing = self._find()
        if existing is not None:
            return existing
        self._add(_secrets.token_bytes(KEY_BYTES))
        created = self._find()
        if created is None:
            raise LocalKeyError("keychain: master key stored but could not be read back")
        return created

    def _find(self) -> Optional[bytes]:
        proc = self.runner(
            ["security", "find-generic-password",
             "-s", self.SERVICE, "-a", self.account, "-w"],
            capture_output=True, text=True,
        )
        if proc.returncode != 0:
            return None
        try:
            raw = bytes.fromhex(proc.stdout.strip())
        except ValueError as exc:
            raise LocalKeyError("keychain: master key item is not valid hex") from exc
        if len(raw) != KEY_BYTES:
            raise LocalKeyError("keychain: master key has the wrong length")
        return raw

    def _add(self, raw: bytes) -> None:
        # `security -i` reads its command from stdin — the hex never hits argv.
        line = (
            f"add-generic-password -U -s {self.SERVICE} -a {self.account} "
            f'-j "portunus local vault master key" -w {raw.hex()}\n'
        )
        proc = self.runner(["security", "-i"], input=line, capture_output=True, text=True)
        if proc.returncode != 0:
            raise LocalKeyError(
                f"keychain: could not store master key: {proc.stderr.strip()[:200]}"
            )


class FileKeyProvider:
    """Master key in a 0600 file under the 0700 state home (non-mac fallback)."""

    def __init__(self, path: Optional[Path] = None):
        self.path = Path(path) if path else home() / "local" / "master.key"

    def key(self) -> bytes:
        if self.path.exists():
            try:
                raw = bytes.fromhex(self.path.read_text().strip())
            except ValueError as exc:
                raise LocalKeyError(f"master key file is not valid hex: {self.path}") from exc
            if len(raw) != KEY_BYTES:
                raise LocalKeyError(f"master key file has the wrong length: {self.path}")
            return raw
        self.path.parent.mkdir(parents=True, exist_ok=True)
        try:
            self.path.parent.chmod(0o700)
        except OSError:
            pass
        raw = _secrets.token_bytes(KEY_BYTES)
        fd = os.open(self.path, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
        with os.fdopen(fd, "w") as fh:
            fh.write(raw.hex())
        return raw


class AutoKeyProvider:
    """Keychain when it works, 0600 key file otherwise (with a loud warning).

    An *explicit* ``PORTUNUS_KEY_PROVIDER=keychain`` stays strict and fails
    closed; auto mode degrades to the file provider so headless accounts (e.g.
    an SSH-only worker user with no login keychain) still get encryption at
    rest instead of nothing. Once a file key exists it keeps being used, so
    the master key never flaps between stores.
    """

    def __init__(self, runner=None):
        self._keychain = KeychainKeyProvider(runner=runner)
        self._file = FileKeyProvider()

    def key(self) -> bytes:
        if self._file.path.exists():
            return self._file.key()
        try:
            return self._keychain.key()
        except LocalKeyError as exc:
            print(
                f"secrets: warning: macOS Keychain unavailable ({exc}); "
                f"falling back to 0600 master-key file at {self._file.path}",
                file=sys.stderr,
            )
            return self._file.key()


def default_key_provider(runner=None):
    """Pick the master-key provider: env override, else Keychain-with-fallback on macOS."""
    forced = os.environ.get("PORTUNUS_KEY_PROVIDER", "").lower()
    if forced == "file":
        return FileKeyProvider()
    if forced == "keychain":
        return KeychainKeyProvider(runner=runner)
    if forced:
        raise LocalKeyError(f"unknown PORTUNUS_KEY_PROVIDER: {forced!r} (want keychain|file)")
    if sys.platform == "darwin" and shutil.which("security"):
        return AutoKeyProvider(runner=runner)
    return FileKeyProvider()


# --- authenticated encryption (stdlib primitives) ---------------------------
def _b64(raw: bytes) -> str:
    return base64.b64encode(raw).decode()


def _unb64(text: str) -> bytes:
    return base64.b64decode(text.encode())


def _derive(master: bytes, label: bytes, nonce: bytes) -> bytes:
    return hmac.new(master, label + nonce, hashlib.sha256).digest()


def _keystream(key: bytes, nonce: bytes, length: int) -> bytes:
    out = bytearray()
    counter = 0
    while len(out) < length:
        out.extend(hmac.new(key, nonce + counter.to_bytes(8, "big"), hashlib.sha256).digest())
        counter += 1
    return bytes(out[:length])


def _xor(data: bytes, stream: bytes) -> bytes:
    return bytes(a ^ b for a, b in zip(data, stream))


def _mac(master: bytes, nonce: bytes, aad: bytes, ct: bytes) -> str:
    mac_key = _derive(master, b"portunus.mac.", nonce)
    return hmac.new(
        mac_key, len(aad).to_bytes(4, "big") + aad + ct, hashlib.sha256
    ).hexdigest()


def seal(master: bytes, aad: bytes, plaintext: bytes) -> dict:
    """Encrypt + authenticate `plaintext`, binding it to `aad`. Returns a blob dict."""
    nonce = _secrets.token_bytes(16)
    enc_key = _derive(master, b"portunus.enc.", nonce)
    ct = _xor(plaintext, _keystream(enc_key, nonce, len(plaintext)))
    return {"alg": ALG, "nonce": _b64(nonce), "ct": _b64(ct), "tag": _mac(master, nonce, aad, ct)}


def open_sealed(master: bytes, aad: bytes, blob: dict) -> bytes:
    """Verify then decrypt a blob produced by ``seal``. Fails closed on any mismatch."""
    if blob.get("alg") != ALG:
        raise VaultIntegrityError(f"unknown algorithm: {blob.get('alg')!r}")
    try:
        nonce = _unb64(blob["nonce"])
        ct = _unb64(blob["ct"])
    except (KeyError, ValueError) as exc:
        raise VaultIntegrityError("malformed vault blob") from exc
    if not hmac.compare_digest(_mac(master, nonce, aad, ct), str(blob.get("tag", ""))):
        raise VaultIntegrityError("MAC mismatch — blob tampered with, or wrong master key")
    enc_key = _derive(master, b"portunus.enc.", nonce)
    return _xor(ct, _keystream(enc_key, nonce, len(ct)))


# --- the vault ---------------------------------------------------------------
class LocalVault:
    """Encrypted-at-rest secret store; implements the ``SecretBackend`` protocol.

    One 0600 JSON file per secret under ``<PORTUNUS_HOME>/vault/`` (0700). A
    file holds ciphertext versions + non-secret metadata only. ``access()``
    returns the latest version's plaintext — call it only from the
    resolver/broker boundary path.
    """

    def __init__(self, directory: Optional[Path] = None, key_provider=None):
        self.dir = Path(directory) if directory else home() / "vault"
        self.dir.mkdir(parents=True, exist_ok=True)
        try:
            self.dir.chmod(0o700)
        except OSError:
            pass
        self._provider = key_provider or default_key_provider()
        self._master: Optional[bytes] = None

    # --- internals -------------------------------------------------------
    def _key(self) -> bytes:
        if self._master is None:
            self._master = self._provider.key()
        return self._master

    def _path(self, sm_name: str) -> Path:
        if not _NAME_RE.match(sm_name or ""):
            raise BackendError(f"invalid secret name: {sm_name!r}")
        return self.dir / f"{sm_name}.json"

    def _load(self, sm_name: str) -> Optional[dict]:
        path = self._path(sm_name)
        if not path.exists():
            return None
        try:
            return json.loads(path.read_text())
        except (json.JSONDecodeError, OSError) as exc:
            raise BackendError(f"unreadable vault file for {sm_name}") from exc

    def _save(self, sm_name: str, doc: dict) -> None:
        path = self._path(sm_name)
        tmp = path.with_suffix(".json.tmp")
        fd = os.open(tmp, os.O_CREAT | os.O_WRONLY | os.O_TRUNC, 0o600)
        with os.fdopen(fd, "w") as fh:
            fh.write(json.dumps(doc, indent=2))
        os.replace(tmp, path)
        os.chmod(path, 0o600)

    @staticmethod
    def _aad(sm_name: str, version: int) -> bytes:
        return f"{sm_name}:{version}".encode()

    # --- writes ----------------------------------------------------------
    def add_version(self, sm_name: str, value: str, meta: Optional[Dict[str, str]] = None) -> int:
        """Encrypt `value` as the next version of `sm_name`. Returns the version number."""
        doc = self._load(sm_name) or {
            "v": 1, "sm_name": sm_name, "created": int(time.time()),
            "meta": {}, "versions": [],
        }
        version = len(doc["versions"]) + 1
        blob = seal(self._key(), self._aad(sm_name, version), value.encode())
        blob["n"] = version
        blob["created"] = int(time.time())
        doc["versions"].append(blob)
        if meta:
            doc["meta"].update({k: v for k, v in meta.items() if v})
        self._save(sm_name, doc)
        return version

    def delete(self, sm_name: str) -> bool:
        """Remove a secret's vault file (ciphertext only ever touched disk)."""
        path = self._path(sm_name)
        if not path.exists():
            return False
        path.unlink()
        return True

    # --- reads (names/metadata are safe; access() is the boundary) --------
    def access(self, sm_name: str) -> str:
        """SecretBackend protocol: latest plaintext for `sm_name`, or BackendError."""
        doc = self._load(sm_name)
        if not doc or not doc.get("versions"):
            raise BackendError(f"secret not found in local vault: {sm_name}")
        blob = doc["versions"][-1]
        try:
            raw = open_sealed(self._key(), self._aad(sm_name, int(blob["n"])), blob)
        except VaultIntegrityError as exc:
            raise BackendError(f"local vault integrity failure for {sm_name}: {exc}") from exc
        except LocalKeyError as exc:
            raise BackendError(f"master key unavailable: {exc}") from exc
        return raw.decode()

    def latest_version(self, sm_name: str) -> int:
        doc = self._load(sm_name)
        if not doc or not doc.get("versions"):
            raise BackendError(f"secret not found in local vault: {sm_name}")
        return int(doc["versions"][-1]["n"])

    def meta(self, sm_name: str) -> Dict[str, str]:
        doc = self._load(sm_name)
        return dict((doc or {}).get("meta", {}))

    def names(self) -> List[str]:
        return sorted(p.stem for p in self.dir.glob("*.json"))
