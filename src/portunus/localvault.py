"""ARCA local-encrypted tier (DOS-726 Stage 1).

The Stage 1 MVP backend: secrets are encrypted at rest with a harness-local
master key, so agents can use them with zero plaintext ever entering an LLM
context, ``.env``, or a cloud provider. We deliberately reuse a vetted, high-
level crypto recipe (``cryptography``'s Fernet: AES-128-CBC + HMAC-SHA256)
instead of hand-rolling anything — Portunus decides *policy*, never *cipher*.

The master key lives in its own 0600 file, separate from the encrypted vault
file, and is never written to the registry, the audit log, or returned by any
method here except the plaintext secret itself (which only the resolver's
boundary sinks may touch).
"""
from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Dict, Optional

from cryptography.fernet import Fernet, InvalidToken

from .backend import BackendError
from .paths import home


class LocalEncryptedBackend:
    """ARCA local-encrypted tier: an encrypted-at-rest ``SecretBackend``.

    Values are stored (encrypted) in ``vault_path``; the master key lives in
    ``key_path``. Both default to files under the shared Portunus state home
    and are created 0600. This is the harness-side "drop" target: the CLI's
    ``drop`` command calls ``store()`` directly so a value can be handed to
    Arca without a round-trip through the resolver or an LLM turn.
    """

    def __init__(self, vault_path: Optional[Path] = None, key_path: Optional[Path] = None):
        base = home()
        self.vault_path = Path(vault_path) if vault_path else base / "vault.enc.json"
        self.key_path = Path(key_path) if key_path else base / "master.key"
        self._fernet = Fernet(self._load_or_create_key())

    # --- master key --------------------------------------------------------
    def _load_or_create_key(self) -> bytes:
        if self.key_path.exists():
            return self.key_path.read_bytes().strip()
        key = Fernet.generate_key()
        self.key_path.parent.mkdir(parents=True, exist_ok=True)
        self.key_path.write_bytes(key)
        os.chmod(self.key_path, 0o600)
        return key

    # --- vault persistence ---------------------------------------------------
    def _load(self) -> Dict[str, str]:
        if not self.vault_path.exists():
            return {}
        try:
            return json.loads(self.vault_path.read_text() or "{}")
        except (OSError, json.JSONDecodeError):
            return {}

    def _flush(self, data: Dict[str, str]) -> None:
        self.vault_path.parent.mkdir(parents=True, exist_ok=True)
        tmp = self.vault_path.with_suffix(".tmp")
        tmp.write_text(json.dumps(data))
        os.chmod(tmp, 0o600)
        os.replace(tmp, self.vault_path)
        os.chmod(self.vault_path, 0o600)

    # --- SecretBackend + drop/remove ---------------------------------------
    def store(self, sm_name: str, value: str) -> None:
        """Encrypt and persist `value` under `sm_name`. The harness-side DROP path."""
        data = self._load()
        data[sm_name] = self._fernet.encrypt(value.encode()).decode()
        self._flush(data)

    def access(self, sm_name: str) -> str:
        data = self._load()
        token = data.get(sm_name)
        if token is None:
            raise BackendError(f"unknown secret: {sm_name}")
        try:
            return self._fernet.decrypt(token.encode()).decode()
        except InvalidToken as exc:
            raise BackendError(
                f"local vault: cannot decrypt {sm_name} (wrong master key or corrupt data)"
            ) from exc

    def remove(self, sm_name: str) -> bool:
        data = self._load()
        existed = data.pop(sm_name, None) is not None
        if existed:
            self._flush(data)
        return existed
