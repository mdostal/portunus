"""Recency-aware, pull-only sync-down cache (story 03, portunus-vault-routing).
GCloudBackend.latest_version() + SyncingBackend -- GCP -> local only, never
the reverse."""
import json
from types import SimpleNamespace

import pytest

from portunus.backend import GcloudBackend, SyncingBackend, VaultBinding
from portunus.localvault import LocalEncryptedBackend


def _mock_gcloud_runner(responses):
    """responses: list of (returncode, stdout, stderr) consumed in call order."""
    calls = []

    def runner(cmd, capture_output, text, timeout):
        calls.append(cmd)
        rc, out, err = responses.pop(0)
        return SimpleNamespace(returncode=rc, stdout=out, stderr=err)

    return runner, calls


def test_latest_version_calls_versions_describe_and_returns_create_time():
    runner, calls = _mock_gcloud_runner([
        (0, json.dumps({"name": "projects/1/secrets/X/versions/3", "createTime": "2026-08-14T00:00:00Z"}), ""),
    ])
    backend = GcloudBackend(runner=runner)
    marker = backend.latest_version("X", project="demo")
    assert marker == "2026-08-14T00:00:00Z"
    cmd = calls[0]
    assert cmd[:5] == ["gcloud", "secrets", "versions", "describe", "latest"]
    assert "--secret=X" in cmd
    assert "--project=demo" in cmd
    assert "--format=json" in cmd


def test_latest_version_never_touches_the_value():
    """The describe call's own response never carries a value -- only
    version/time metadata."""
    runner, calls = _mock_gcloud_runner([
        (0, json.dumps({"name": "projects/1/secrets/X/versions/3", "createTime": "T1"}), ""),
    ])
    backend = GcloudBackend(runner=runner)
    backend.latest_version("X")
    assert len(calls) == 1
    assert "access" not in calls[0]


def test_syncing_backend_first_access_fetches_and_caches(home):
    remote_runner, calls = _mock_gcloud_runner([
        (0, json.dumps({"name": "v1", "createTime": "T1"}), ""),
        (0, "REAL-VALUE", ""),
    ])
    remote = GcloudBackend(runner=remote_runner)
    local = LocalEncryptedBackend()
    sync = SyncingBackend(remote, local, home / "sync-state.json")

    value = sync.access("X", project="demo")
    assert value == "REAL-VALUE"
    # cached locally under a project-prefixed key
    assert local.access("demo:X") == "REAL-VALUE"


def test_syncing_backend_second_access_serves_from_cache_when_fresh(home):
    remote_runner, calls = _mock_gcloud_runner([
        (0, json.dumps({"name": "v1", "createTime": "T1"}), ""),
        (0, "REAL-VALUE", ""),
        (0, json.dumps({"name": "v1", "createTime": "T1"}), ""),  # second latest_version check
    ])
    remote = GcloudBackend(runner=remote_runner)
    local = LocalEncryptedBackend()
    sync = SyncingBackend(remote, local, home / "sync-state.json")

    sync.access("X", project="demo")
    value = sync.access("X", project="demo")
    assert value == "REAL-VALUE"
    # only 3 calls total: 2 for the first access (version check + value fetch),
    # 1 for the second access's version check -- the value is NEVER re-fetched
    assert len(calls) == 3


def test_syncing_backend_refetches_when_remote_version_changed(home):
    remote_runner, calls = _mock_gcloud_runner([
        (0, json.dumps({"name": "v1", "createTime": "T1"}), ""),
        (0, "OLD-VALUE", ""),
        (0, json.dumps({"name": "v2", "createTime": "T2"}), ""),  # rotated
        (0, "NEW-VALUE", ""),
    ])
    remote = GcloudBackend(runner=remote_runner)
    local = LocalEncryptedBackend()
    sync = SyncingBackend(remote, local, home / "sync-state.json")

    first = sync.access("X", project="demo")
    second = sync.access("X", project="demo")
    assert first == "OLD-VALUE"
    assert second == "NEW-VALUE"
    assert local.access("demo:X") == "NEW-VALUE"


def test_syncing_backend_falls_back_to_always_fetch_when_remote_has_no_latest_version(home):
    """A remote backend without latest_version() (e.g. a bare MockBackend)
    always fetches live and still caches -- correct, just not optimally
    cached."""
    from portunus.backend import MockBackend
    remote = MockBackend({"X": "MOCK-VALUE"})
    local = LocalEncryptedBackend()
    sync = SyncingBackend(remote, local, home / "sync-state.json")

    value = sync.access("X", project="demo")
    assert value == "MOCK-VALUE"
    assert local.access("demo:X") == "MOCK-VALUE"


def test_syncing_backend_project_prefixed_key_never_collides_with_local_drop(home):
    """A genuinely-local portunus_drop-ped secret sharing the same sm_name
    as a cached remote copy must not collide (grill H1)."""
    local = LocalEncryptedBackend()
    local.store("X", "GENUINELY-LOCAL-VALUE")  # e.g. from portunus_drop directly

    remote_runner, _ = _mock_gcloud_runner([
        (0, json.dumps({"name": "v1", "createTime": "T1"}), ""),
        (0, "CACHED-REMOTE-VALUE", ""),
    ])
    remote = GcloudBackend(runner=remote_runner)
    sync = SyncingBackend(remote, local, home / "sync-state.json")
    sync.access("X", project="demo")

    assert local.access("X") == "GENUINELY-LOCAL-VALUE"
    assert local.access("demo:X") == "CACHED-REMOTE-VALUE"


def test_last_sync_result_reports_synced_then_fresh(home):
    remote_runner, _ = _mock_gcloud_runner([
        (0, json.dumps({"name": "v1", "createTime": "T1"}), ""),
        (0, "VALUE", ""),
        (0, json.dumps({"name": "v1", "createTime": "T1"}), ""),
    ])
    remote = GcloudBackend(runner=remote_runner)
    local = LocalEncryptedBackend()
    sync = SyncingBackend(remote, local, home / "sync-state.json")

    sync.access("X", project="demo")
    assert sync.last_sync_result == "synced"
    sync.access("X", project="demo")
    assert sync.last_sync_result == "fresh"


def test_sync_state_file_never_holds_a_value(home):
    remote_runner, _ = _mock_gcloud_runner([
        (0, json.dumps({"name": "v1", "createTime": "T1"}), ""),
        (0, "SUPER-SECRET-VALUE", ""),
    ])
    remote = GcloudBackend(runner=remote_runner)
    local = LocalEncryptedBackend()
    state_path = home / "sync-state.json"
    sync = SyncingBackend(remote, local, state_path)
    sync.access("X", project="demo")

    assert "SUPER-SECRET-VALUE" not in state_path.read_text()


# --- story 02 (portunus-swappable-trio): offline-resilient fallback -----

def test_serves_cached_value_when_remote_unreachable(home):
    """First sync succeeds and caches; a later access whose latest_version()
    check fails (simulated network outage) still returns the cached value
    instead of raising."""
    from portunus.backend import BackendError

    class FlakyRemote:
        def __init__(self):
            self.calls = 0

        def latest_version(self, sm_name, project=""):
            self.calls += 1
            if self.calls == 1:
                return "T1"
            raise BackendError("network unreachable")

        def access(self, sm_name, project=""):
            return "REAL-VALUE"

    remote = FlakyRemote()
    local = LocalEncryptedBackend()
    sync = SyncingBackend(remote, local, home / "sync-state.json")

    first = sync.access("X", project="demo")
    assert first == "REAL-VALUE"
    assert sync.last_sync_result == "synced"

    second = sync.access("X", project="demo")
    assert second == "REAL-VALUE"
    assert sync.last_sync_result == "stale-offline"


def test_no_fallback_available_propagates_original_error(home):
    """Never synced before AND remote unreachable -- genuinely nothing to
    serve, the original BackendError propagates."""
    from portunus.backend import BackendError

    class AlwaysUnreachable:
        def latest_version(self, sm_name, project=""):
            raise BackendError("network unreachable")

        def access(self, sm_name, project=""):
            raise AssertionError("should never be called")

    remote = AlwaysUnreachable()
    local = LocalEncryptedBackend()
    sync = SyncingBackend(remote, local, home / "sync-state.json")

    with pytest.raises(BackendError):
        sync.access("X", project="demo")


def test_stale_offline_serve_does_not_update_sync_state_marker(home):
    """A stale-offline serve must not falsely mark the cache as
    verified-fresh -- the marker stays at the last REAL confirmation."""
    from portunus.backend import BackendError
    import json as _json

    class FlakyRemote:
        def __init__(self):
            self.calls = 0

        def latest_version(self, sm_name, project=""):
            self.calls += 1
            if self.calls == 1:
                return "T1"
            raise BackendError("network unreachable")

        def access(self, sm_name, project=""):
            return "REAL-VALUE"

    remote = FlakyRemote()
    local = LocalEncryptedBackend()
    state_path = home / "sync-state.json"
    sync = SyncingBackend(remote, local, state_path)

    sync.access("X", project="demo")
    state_after_first = _json.loads(state_path.read_text())

    sync.access("X", project="demo")  # stale-offline serve
    state_after_second = _json.loads(state_path.read_text())

    assert state_after_first == state_after_second


def test_value_fetch_failure_after_successful_version_check_is_unchanged(home):
    """A real (non-connectivity) failure during the value fetch itself --
    e.g. permission denied -- must NOT be swallowed as 'offline'."""
    from portunus.backend import BackendError

    class PermissionDenied:
        def latest_version(self, sm_name, project=""):
            return "T1"

        def access(self, sm_name, project=""):
            raise BackendError("permission denied")

    remote = PermissionDenied()
    local = LocalEncryptedBackend()
    sync = SyncingBackend(remote, local, home / "sync-state.json")

    with pytest.raises(BackendError, match="permission denied"):
        sync.access("X", project="demo")
