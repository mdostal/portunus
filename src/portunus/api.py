"""Programmatic Portunus session API.

This layer is the public boundary for browser/login session restore flows. Arca
owns encrypted persistence; the API owns lifecycle policy such as TTL refusal.
"""
from __future__ import annotations

from datetime import datetime, timezone
from typing import Any, Dict, List, Optional

from .backend import BackendError
from .localvault import LocalEncryptedBackend


class SessionExpiredError(BackendError):
    """Raised when a stored session exists but its TTL has elapsed."""


def save_session(
    site: str,
    account: str,
    session: Any,
    *,
    ttl_seconds: int,
    backend: Optional[LocalEncryptedBackend] = None,
    rotation_interval_seconds: Optional[int] = None,
    rotation_generation: int = 1,
) -> Dict[str, Any]:
    """Persist a session through Arca and return non-secret metadata."""
    vault = _backend(backend)
    metadata = vault.store_session(
        site,
        account,
        session,
        ttl_seconds=ttl_seconds,
        rotation_interval_seconds=rotation_interval_seconds,
        rotation_generation=rotation_generation,
    )
    return _public_metadata(metadata)


def load_session(
    site: str,
    account: str,
    *,
    backend: Optional[LocalEncryptedBackend] = None,
) -> Any:
    """Return an active session payload, refusing expired records."""
    vault = _backend(backend)
    record = vault.load_session(site, account)
    metadata = _public_metadata(record)
    if _is_expired(metadata):
        raise SessionExpiredError(f"session expired: {metadata['id']}")
    return record["session"]


def list_sessions(
    *,
    backend: Optional[LocalEncryptedBackend] = None,
    include_expired: bool = False,
) -> List[Dict[str, Any]]:
    """List session metadata without returning cookies, tokens, or storage."""
    vault = _backend(backend)
    sessions = []
    for metadata in vault.list_sessions():
        public = _public_metadata(metadata)
        public["expired"] = _is_expired(public)
        if public["expired"] and not include_expired:
            continue
        sessions.append(public)
    return sorted(sessions, key=lambda item: item["id"])


def revoke_session(
    site: str,
    account: str,
    *,
    backend: Optional[LocalEncryptedBackend] = None,
) -> bool:
    """Permanently remove a stored session from Arca."""
    return _backend(backend).remove_session(site, account)


def _backend(backend: Optional[LocalEncryptedBackend]) -> LocalEncryptedBackend:
    return backend if backend is not None else LocalEncryptedBackend()


def _public_metadata(record: Dict[str, Any]) -> Dict[str, Any]:
    namespace = _dict_field(record, "namespace")
    site = _string_field(namespace, "site")
    account = _string_field(namespace, "account")
    return {
        "id": f"{site}/{account}",
        "schema": _string_field(record, "schema"),
        "namespace": {"site": site, "account": account},
        "ttl": dict(_dict_field(record, "ttl")),
        "rotation": dict(_dict_field(record, "rotation")),
    }


def _is_expired(metadata: Dict[str, Any]) -> bool:
    expires_at = _string_field(_dict_field(metadata, "ttl"), "expires_at")
    return _parse_time(expires_at) <= _utc_now()


def _dict_field(record: Dict[str, Any], field: str) -> Dict[str, Any]:
    value = record.get(field)
    if not isinstance(value, dict):
        raise BackendError(f"invalid session metadata: {field}")
    return value


def _string_field(record: Dict[str, Any], field: str) -> str:
    value = record.get(field)
    if not isinstance(value, str) or not value:
        raise BackendError(f"invalid session metadata: {field}")
    return value


def _parse_time(value: str) -> datetime:
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError as exc:
        raise BackendError(f"invalid session expiry: {value}") from exc
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc)


def _utc_now() -> datetime:
    return datetime.now(timezone.utc).replace(microsecond=0)
