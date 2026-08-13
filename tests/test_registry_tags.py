"""Structured tag schema, fail-closed resolve_by_tags, migration, and the
registry write lock (story 01-tag-schema-migration-lock)."""
import fcntl
import threading

import pytest

from portunus import Registry
from portunus.registry import NoMatch, AmbiguousMatch, RegistryLocked


def test_resolve_by_tags_exact_match(home):
    reg = Registry()
    reg.add("vercel-mdostal", "sm-vercel-mdostal", provider="vercel", project="mdostal.com", env="prod")
    reg.add("vercel-other", "sm-vercel-other", provider="vercel", project="other.com", env="prod")
    ref = reg.resolve_by_tags(provider="vercel", project="mdostal.com")
    assert ref.name == "vercel-mdostal"


def test_resolve_by_tags_no_match_raises(home):
    reg = Registry()
    reg.add("vercel-mdostal", "sm-vercel-mdostal", provider="vercel", project="mdostal.com")
    with pytest.raises(NoMatch):
        reg.resolve_by_tags(provider="aws")


def test_resolve_by_tags_ambiguous_raises_with_candidates(home):
    reg = Registry()
    reg.add("a", "sm-a", provider="vercel", project="mdostal.com", env="prod")
    reg.add("b", "sm-b", provider="vercel", project="mdostal.com", env="staging")
    with pytest.raises(AmbiguousMatch) as exc_info:
        reg.resolve_by_tags(provider="vercel", project="mdostal.com")
    assert set(exc_info.value.candidates) == {"a", "b"}


def test_resolve_by_tags_near_identical_references_stay_ambiguous(home):
    """The single most important test in this story: a query that could look like it
    matches one reference must not silently resolve when a near-identical reference
    exists -- only a fully-specified query should disambiguate."""
    reg = Registry()
    reg.add("prod-ref", "sm-prod", provider="vercel", project="mdostal.com", env="prod")
    reg.add("staging-ref", "sm-staging", provider="vercel", project="mdostal.com", env="staging")
    with pytest.raises(AmbiguousMatch):
        reg.resolve_by_tags(provider="vercel", project="mdostal.com")
    ref = reg.resolve_by_tags(provider="vercel", project="mdostal.com", env="prod")
    assert ref.name == "prod-ref"


def test_resolve_by_tags_open_tags_dict(home):
    reg = Registry()
    reg.add("x", "sm-x", tags={"team": "platform"})
    reg.add("y", "sm-y", tags={"team": "growth"})
    ref = reg.resolve_by_tags(team="platform")
    assert ref.name == "x"


def test_resolve_by_tags_never_substring_matches(home):
    """Matcher must be exact-value, not substring -- a substring match would be a
    silent-ambiguity bug (the highest risk flagged for this whole epic)."""
    reg = Registry()
    reg.add("x", "sm-x", provider="vercel")
    with pytest.raises(NoMatch):
        reg.resolve_by_tags(provider="verc")


def test_migrate_legacy_tags_is_additive(home):
    reg = Registry()
    reg.add("legacy", "sm-legacy", scope="shared", kind="anthropic")
    count = reg.migrate_legacy_tags()
    assert count == 1
    ref = reg.require("legacy")
    assert ref.scope == "shared"
    assert ref.kind == "anthropic"
    assert ref.tags.get("scope") == "shared"
    assert ref.tags.get("kind") == "anthropic"


def test_migrate_legacy_tags_is_idempotent(home):
    reg = Registry()
    reg.add("legacy", "sm-legacy", scope="shared", kind="anthropic")
    first = reg.migrate_legacy_tags()
    second = reg.migrate_legacy_tags()
    assert first == 1
    assert second == 0


def test_migrate_legacy_tags_skips_already_tagged(home):
    reg = Registry()
    reg.add("tagged", "sm-tagged", provider="vercel", project="mdostal.com")
    count = reg.migrate_legacy_tags()
    assert count == 0


def test_lock_acquisition_timeout_raises_registry_locked(home):
    reg = Registry(lock_timeout=0.2)
    lock_path = reg.lock_path
    lock_path.parent.mkdir(parents=True, exist_ok=True)
    fh = open(lock_path, "w")
    fcntl.flock(fh, fcntl.LOCK_EX)
    try:
        with pytest.raises(RegistryLocked):
            reg.add("x", "sm-x")
    finally:
        fcntl.flock(fh, fcntl.LOCK_UN)
        fh.close()


def test_concurrent_writers_do_not_lose_updates(home):
    """Two independent Registry instances (simulating two processes/threads)
    writing concurrently must not lose either write."""
    errors = []

    def writer(n):
        try:
            reg = Registry()
            for i in range(5):
                reg.add(f"ref-{n}-{i}", f"sm-{n}-{i}")
        except Exception as exc:  # pragma: no cover - surfaced via errors list
            errors.append(exc)

    threads = [threading.Thread(target=writer, args=(n,)) for n in range(4)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    assert not errors
    final = Registry()
    assert len(final) == 20
