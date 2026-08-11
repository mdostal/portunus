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
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Dict, Optional
from urllib.parse import quote

from cryptography.fernet import Fernet, InvalidToken

from .backend import BackendError
from .paths import home

SESSION_SCHEMA = "portunus.session.v1"


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

    # --- browser/login session storage -------------------------------------
    @staticmethod
    def session_key(site: str, account: str) -> str:
        """Return the stable Arca namespace for a site/account session."""
        site = _namespace_part(site, "site")
        account = _namespace_part(account, "account")
        return f"session:{quote(site, safe='')}:{quote(account, safe='')}"

    def store_session(
        self,
        site: str,
        account: str,
        session: Any,
        *,
        ttl_seconds: int,
        owner_role: Optional[str] = None,
        rotation_interval_seconds: Optional[int] = None,
        rotation_generation: int = 1,
    ) -> Dict[str, Any]:
        """Encrypt and persist a browser/login session with TTL metadata.

        ``session`` may be a Playwright-style storageState dict or another
        JSON-serializable session object. The returned inspection view contains
        only namespace and lifecycle metadata, never cookies or token payloads.
        """
        ttl_seconds = _positive_int(ttl_seconds, "ttl_seconds")
        rotation_generation = _positive_int(rotation_generation, "rotation_generation")
        if rotation_interval_seconds is not None:
            rotation_interval_seconds = _positive_int(
                rotation_interval_seconds, "rotation_interval_seconds"
            )

        site = _namespace_part(site, "site")
        account = _namespace_part(account, "account")
        created_at = _utc_now()
        expires_at = created_at + timedelta(seconds=ttl_seconds)
        rotate_after = (
            created_at + timedelta(seconds=rotation_interval_seconds)
            if rotation_interval_seconds is not None
            else None
        )
        namespace_dict = {"site": site, "account": account}
        if owner_role:
            namespace_dict["owner_role"] = owner_role

        record = {
            "schema": SESSION_SCHEMA,
            "namespace": namespace_dict,
            "ttl": {"seconds": ttl_seconds, "expires_at": _format_time(expires_at)},
            "rotation": {
                "generation": rotation_generation,
                "last_rotated_at": _format_time(created_at),
                "interval_seconds": rotation_interval_seconds,
                "rotate_after": _format_time(rotate_after) if rotate_after else None,
            },
            "session": session,
        }
        try:
            payload = json.dumps(record, sort_keys=True, separators=(",", ":"))
        except (TypeError, ValueError):
            raise ValueError("session must be JSON-serializable") from None
        self.store(self.session_key(site, account), payload)
        return self.inspect_session(site, account)

    def load_session(self, site: str, account: str) -> Dict[str, Any]:
        """Decrypt and return a complete stored session record."""
        key = self.session_key(site, account)
        try:
            record = json.loads(self.access(key))
        except json.JSONDecodeError as exc:
            raise BackendError(f"local vault: invalid session record for {key}") from exc
        if not isinstance(record, dict) or record.get("schema") != SESSION_SCHEMA:
            raise BackendError(f"local vault: invalid session record for {key}")
        return record

    def inspect_session(self, site: str, account: str) -> Dict[str, Any]:
        """Return non-secret namespace, TTL, and rotation metadata."""
        record = self.load_session(site, account)
        return {
            "schema": record["schema"],
            "namespace": dict(record["namespace"]),
            "ttl": dict(record["ttl"]),
            "rotation": dict(record["rotation"]),
        }

    def remove_session(self, site: str, account: str) -> bool:
        """Remove a stored browser/login session."""
        return self.remove(self.session_key(site, account))


def _namespace_part(value: str, field: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"{field} must be a non-empty string")
    return value.strip()


def _positive_int(value: int, field: str) -> int:
    if not isinstance(value, int) or isinstance(value, bool) or value <= 0:
        raise ValueError(f"{field} must be a positive integer")
    return value


def _utc_now() -> datetime:
    return datetime.now(timezone.utc).replace(microsecond=0)


def _format_time(value: datetime) -> str:
    return value.isoformat().replace("+00:00", "Z")
