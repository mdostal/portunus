"""Playwright storage-state injection backed by Portunus sessions.

The adapter is intentionally duck-typed instead of importing Playwright. That
keeps Portunus usable without a browser dependency while still matching the
Python Playwright contract: ``browser.new_context(storage_state=...)``.
"""
from __future__ import annotations

import copy
from datetime import datetime, timezone
from typing import Any, Callable, Dict, Optional

from .backend import BackendError
from .broker import Broker, NotInjectable
from .localvault import LocalEncryptedBackend
from .registry import Registry

PLAYWRIGHT_SESSION_KIND = "playwright-storage-state"


class SessionUnavailable(RuntimeError):
    """Raised when a stored browser session cannot be injected."""


class SessionExpired(SessionUnavailable):
    """Raised when a stored session is past its TTL."""


class SessionRevoked(SessionUnavailable):
    """Raised when a stored session has been revoked and removed."""


class EphemeralPlaywrightContext:
    """Proxy a Playwright BrowserContext and scrub injected state on close."""

    def __init__(self, context: Any, storage_state: Dict[str, Any]):
        self._context = context
        self._storage_state: Optional[Dict[str, Any]] = storage_state

    @property
    def raw_context(self) -> Any:
        return self._context

    def close(self) -> Any:
        try:
            close = getattr(self._context, "close", None)
            if close is not None:
                return close()
            return None
        finally:
            self._drop_storage_state()

    def _drop_storage_state(self) -> None:
        if self._storage_state is not None:
            _scrub_storage_state(self._storage_state)
            self._storage_state = None

    def __enter__(self) -> "EphemeralPlaywrightContext":
        return self

    def __exit__(self, exc_type: object, exc: object, tb: object) -> None:
        self.close()

    def __getattr__(self, name: str) -> Any:
        return getattr(self._context, name)


class PlaywrightSessionAdapter:
    """Save, load, revoke, and inject Playwright ``storageState`` records."""

    def __init__(
        self,
        backend: LocalEncryptedBackend,
        *,
        registry: Optional[Registry] = None,
        broker: Optional[Broker] = None,
        now: Optional[Callable[[], datetime]] = None,
    ):
        self.backend = backend
        self.registry = registry
        self.broker = broker
        self._now = now or _utc_now

    @staticmethod
    def reference_name(site: str, account: str) -> str:
        return LocalEncryptedBackend.session_key(site, account)

    def save_storage_state(
        self,
        site: str,
        account: str,
        storage_state: Dict[str, Any],
        *,
        ttl_seconds: int,
        rotation_interval_seconds: Optional[int] = None,
    ) -> Dict[str, Any]:
        """Persist a Playwright storage state and register its injectable ref."""
        if not isinstance(storage_state, dict):
            raise ValueError("storage_state must be a dict")
        inspection = self.backend.store_session(
            site,
            account,
            copy.deepcopy(storage_state),
            ttl_seconds=ttl_seconds,
            rotation_interval_seconds=rotation_interval_seconds,
        )
        self._register(site, account, state="enabled")
        return inspection

    def load_storage_state(self, site: str, account: str) -> Dict[str, Any]:
        """Return a non-expired Playwright storage state for injection."""
        self._check_injectable(site, account)
        record = self.backend.load_session(site, account)
        self._check_not_expired(site, account, record)
        session = record.get("session")
        if not isinstance(session, dict):
            raise BackendError(
                f"local vault: invalid Playwright storageState for "
                f"{self.reference_name(site, account)}"
            )
        return copy.deepcopy(session)

    def new_context(self, browser: Any, site: str, account: str, **kwargs: Any) -> EphemeralPlaywrightContext:
        """Create a BrowserContext with Portunus-managed storage state.

        The plaintext storage state lives only in memory and is cleared when
        the returned context is closed.
        """
        if "storage_state" in kwargs:
            raise ValueError("storage_state is managed by Portunus")
        storage_state = self.load_storage_state(site, account)
        context = browser.new_context(storage_state=storage_state, **kwargs)
        return EphemeralPlaywrightContext(context, storage_state)

    def revoke(self, site: str, account: str) -> bool:
        """Mark a session revoked and remove its encrypted storage blob."""
        ref = self._register(site, account, state="revoked")
        if self.registry is not None:
            self.registry.set_state(ref.name, "revoked")
        return self.backend.remove_session(site, account)

    def _register(self, site: str, account: str, *, state: str):
        if self.registry is None:
            return None
        key = self.reference_name(site, account)
        existing = self.registry.get(key)
        return self.registry.add(
            key,
            key,
            scope=site,
            kind=PLAYWRIGHT_SESSION_KIND,
            state=state,
            approval=existing.approval if existing is not None else "",
        )

    def _check_injectable(self, site: str, account: str) -> None:
        if self.registry is None or self.broker is None:
            return
        name = self.reference_name(site, account)
        if self.registry.get(name) is None:
            return
        try:
            self.broker.check_injectable(name)
        except NotInjectable as exc:
            ref = self.registry.require(name)
            if ref.state == "revoked":
                self.backend.remove_session(site, account)
                raise SessionRevoked(
                    f"{name} is revoked; removed stored Playwright session "
                    "and requires re-authentication"
                ) from exc
            raise

    def _check_not_expired(self, site: str, account: str, record: Dict[str, Any]) -> None:
        expires_at = record.get("ttl", {}).get("expires_at")
        expires = _parse_utc(expires_at)
        if expires <= self._now():
            raise SessionExpired(
                f"{self.reference_name(site, account)} expired at "
                f"{expires.isoformat().replace('+00:00', 'Z')}; "
                "re-authentication required"
            )


def _utc_now() -> datetime:
    return datetime.now(timezone.utc).replace(microsecond=0)


def _parse_utc(value: Any) -> datetime:
    if not isinstance(value, str) or not value:
        raise BackendError("local vault: session record is missing ttl.expires_at")
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError as exc:
        raise BackendError(f"local vault: invalid ttl.expires_at {value!r}") from exc
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc).replace(microsecond=0)


def _scrub_storage_state(storage_state: Dict[str, Any]) -> None:
    storage_state.clear()
