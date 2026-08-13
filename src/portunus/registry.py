"""Reference registry — name -> Secret Manager location, NEVER the value.

A ``Reference`` records where a secret lives (its Secret Manager name/path),
its scope/kind, its lifecycle state, and whether access is approval-gated. It
deliberately has no field for the secret value: the registry is safe to read,
copy, and inspect. The value is only ever fetched, at the call boundary, by
the resolver.
"""
from __future__ import annotations

import fcntl
import json
import os
import time
from contextlib import contextmanager
from dataclasses import dataclass, asdict, field
from pathlib import Path
from typing import Dict, Iterator, List, Optional

from .paths import home

# Lifecycle states, mirroring bin/secrets. "enabled"/"locked" are injectable;
# "dropped"/"revoked"/"requested" fail closed. "requested" is an agent-initiated
# placeholder (no value stored yet) -- see Registry.request().
VALID_STATES = ("enabled", "locked", "dropped", "revoked", "requested")

# Tag keys resolved against Reference's own fields; anything else is looked
# up in the open `tags` dict. Keep in sync with Reference's structured fields.
_STRUCTURED_TAG_FIELDS = ("provider", "project", "env", "scope", "kind")


class NoMatch(KeyError):
    """resolve_by_tags() found zero references matching the given tags."""


class AmbiguousMatch(KeyError):
    """resolve_by_tags() found more than one match. Never guesses -- fails closed."""

    def __init__(self, candidates: List[str]):
        self.candidates = candidates
        super().__init__(f"ambiguous match: {candidates!r}")


class RegistryLocked(RuntimeError):
    """Could not acquire the registry write lock within the timeout."""


@dataclass
class Reference:
    """A pointer to a secret. Holds location + policy metadata, never a value."""

    name: str            # the reference / placeholder key, e.g. "shared-anthropic"
    sm_name: str         # Secret Manager secret name, e.g. "dostal-shared-anthropic"
    scope: str = ""      # "shared" or a client slug
    kind: str = ""       # gemini | anthropic | linear | slack | ...
    state: str = "enabled"
    approval: str = ""   # "required" if access is gated
    sm_path: str = ""    # projects/<p>/secrets/<sm_name> (informational)
    provider: str = ""   # e.g. "vercel", "gcp", "github"
    project: str = ""    # e.g. a project/site slug such as "mdostal.com"
    env: str = ""        # e.g. "prod" | "staging" | "dev"
    tags: dict = field(default_factory=dict)  # open, forward-compat key/value tags

    def to_dict(self) -> dict:
        return asdict(self)

    def matches_tag(self, key: str, value: str) -> bool:
        """Exact-value match only -- never substring/fuzzy. See resolve_by_tags()."""
        if key in _STRUCTURED_TAG_FIELDS:
            return getattr(self, key) == value
        return self.tags.get(key) == value


class Registry:
    """A JSON-backed map of reference name -> Reference. 0600 on disk."""

    _LOCK_POLL_INTERVAL = 0.02

    def __init__(self, path: Optional[Path] = None, lock_timeout: float = 5.0):
        self.path = Path(path) if path else home() / "registry.json"
        self.lock_path = self.path.with_suffix(".lock")
        self.lock_timeout = lock_timeout
        self._data: Dict[str, Reference] = {}
        self._load()

    # --- persistence -----------------------------------------------------
    def _load(self) -> None:
        if not self.path.exists():
            self._data = {}
            return
        try:
            raw = json.loads(self.path.read_text() or "{}")
        except (json.JSONDecodeError, OSError):
            raw = {}
        self._data = {k: Reference(**v) for k, v in raw.items()}

    def _flush(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        tmp = self.path.with_suffix(".json.tmp")
        tmp.write_text(json.dumps({k: v.to_dict() for k, v in self._data.items()}, indent=2))
        os.chmod(tmp, 0o600)
        os.replace(tmp, self.path)
        os.chmod(self.path, 0o600)

    @contextmanager
    def _locked(self):
        """Serialize mutation across processes/threads sharing this registry file.

        Acquires an exclusive flock (bounded by lock_timeout, else RegistryLocked),
        reloads the freshest on-disk state so this instance never overwrites a
        sibling writer's update with a stale in-memory copy, yields for the
        mutation, then flushes and releases.
        """
        self.lock_path.parent.mkdir(parents=True, exist_ok=True)
        fh = open(self.lock_path, "w")
        deadline = time.monotonic() + self.lock_timeout
        acquired = False
        try:
            while time.monotonic() < deadline:
                try:
                    fcntl.flock(fh, fcntl.LOCK_EX | fcntl.LOCK_NB)
                    acquired = True
                    break
                except OSError:
                    time.sleep(self._LOCK_POLL_INTERVAL)
            if not acquired:
                raise RegistryLocked(
                    f"could not acquire registry lock within {self.lock_timeout}s "
                    f"({self.lock_path})"
                )
            self._load()
            yield
            self._flush()
        finally:
            if acquired:
                fcntl.flock(fh, fcntl.LOCK_UN)
            fh.close()

    # --- mutation --------------------------------------------------------
    def add(
        self,
        name: str,
        sm_name: str,
        scope: str = "",
        kind: str = "",
        state: str = "enabled",
        approval: str = "",
        project: str = "",
        provider: str = "",
        env: str = "",
        tags: Optional[dict] = None,
    ) -> Reference:
        """Register (or overwrite) a reference. Value is never accepted here."""
        if state not in VALID_STATES:
            raise ValueError(f"invalid state: {state!r} (want one of {VALID_STATES})")
        sm_path = f"projects/{project}/secrets/{sm_name}" if project else ""
        with self._locked():
            ref = Reference(
                name=name, sm_name=sm_name, scope=scope, kind=kind,
                state=state, approval=approval, sm_path=sm_path,
                provider=provider, project=project, env=env, tags=dict(tags or {}),
            )
            self._data[name] = ref
        return ref

    def remove(self, name: str) -> bool:
        with self._locked():
            existed = self._data.pop(name, None) is not None
        return existed

    def set_state(self, name: str, state: str) -> Reference:
        if state not in VALID_STATES:
            raise ValueError(f"invalid state: {state!r}")
        with self._locked():
            ref = self.require(name)
            ref.state = state
        return ref

    def set_approval(self, name: str, required: bool) -> Reference:
        with self._locked():
            ref = self.require(name)
            ref.approval = "required" if required else ""
        return ref

    def migrate_legacy_tags(self) -> int:
        """Additively copy scope/kind into tags{} for references that have no
        tags yet. Never touches scope/kind. Idempotent -- a reference with
        tags already set (from this or a prior migration) is left alone."""
        migrated = 0
        with self._locked():
            for ref in self._data.values():
                if ref.tags:
                    continue
                new_tags = {}
                if ref.scope:
                    new_tags["scope"] = ref.scope
                if ref.kind:
                    new_tags["kind"] = ref.kind
                if new_tags:
                    ref.tags = new_tags
                    migrated += 1
        return migrated

    def request(
        self,
        name: str,
        sm_name: str = "",
        *,
        provider: str = "",
        project: str = "",
        env: str = "",
        tags: Optional[dict] = None,
    ) -> Reference:
        """Create a value-less placeholder reference in state=requested.

        This is an agent-initiated ASK for a secret to be added -- never a
        way to supply one. No value is stored anywhere; state=requested
        fails closed via Broker.check_injectable exactly like dropped/
        revoked, so a placeholder can never accidentally become injectable
        on its own.
        """
        with self._locked():
            ref = Reference(
                name=name, sm_name=sm_name, state="requested",
                provider=provider, project=project, env=env, tags=dict(tags or {}),
            )
            self._data[name] = ref
        return ref

    def retag(
        self,
        name: str,
        *,
        provider: Optional[str] = None,
        project: Optional[str] = None,
        env: Optional[str] = None,
        tags: Optional[dict] = None,
    ) -> Reference:
        """Update a reference's provider/project/env/tags in place.

        Only fields explicitly passed (non-None) change. Rejects any update
        whose resulting tag combination would collide with a DIFFERENT
        existing reference -- reuses matches_tag(), the same exact-match
        logic resolve_by_tags() uses, so there is one collision definition,
        not two. Retagging to a reference's own current tags always
        succeeds (never collides with itself).
        """
        with self._locked():
            ref = self.require(name)
            new_provider = provider if provider is not None else ref.provider
            new_project = project if project is not None else ref.project
            new_env = env if env is not None else ref.env
            new_tags_dict = tags if tags is not None else ref.tags

            full_tags: Dict[str, str] = {}
            for field_name, val in (
                ("provider", new_provider), ("project", new_project), ("env", new_env),
            ):
                if val:
                    full_tags[field_name] = val
            full_tags.update(new_tags_dict)

            colliding = [
                other.name for other in self._data.values()
                if other.name != name and full_tags
                and all(other.matches_tag(k, v) for k, v in full_tags.items())
            ]
            if colliding:
                raise AmbiguousMatch(sorted(colliding + [name]))

            ref.provider = new_provider
            ref.project = new_project
            ref.env = new_env
            ref.tags = dict(new_tags_dict)
        return ref

    # --- lookup ----------------------------------------------------------
    def get(self, name: str) -> Optional[Reference]:
        return self._data.get(name)

    def require(self, name: str) -> Reference:
        ref = self._data.get(name)
        if ref is None:
            raise KeyError(name)
        return ref

    def resolve_by_tags(self, **partial_tags: str) -> Reference:
        """Fail-closed tag resolution: exactly one match or an explicit error.

        Never guesses. Zero matches raises NoMatch; more than one match raises
        AmbiguousMatch (never silently picks one) with every candidate name.
        Matching is exact-value only -- no substring/fuzzy matching, ever.
        """
        matches = [
            ref for ref in self._data.values()
            if all(ref.matches_tag(k, v) for k, v in partial_tags.items())
        ]
        if not matches:
            raise NoMatch(f"no reference matches tags: {partial_tags!r}")
        if len(matches) > 1:
            raise AmbiguousMatch([m.name for m in matches])
        return matches[0]

    def __contains__(self, name: object) -> bool:
        return name in self._data

    def __iter__(self) -> Iterator[Reference]:
        return iter(self._data.values())

    def __len__(self) -> int:
        return len(self._data)
