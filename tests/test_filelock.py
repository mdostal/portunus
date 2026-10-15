"""filelock.flock_path under real cross-process contention -- the one
exclusive-lock loop Registry/AuditChain/LocalEncryptedBackend/views/roles/
rotation bindings all share."""
import os
import subprocess
import sys
import time

import pytest

from portunus import Registry, RegistryLocked
from portunus.filelock import LockTimeout, flock_path


@pytest.fixture
def held_lock(tmp_path):
    """A separate process holding `lock_path` until the test finishes."""
    procs = []

    def hold(lock_path):
        ready = tmp_path / f"{lock_path.name}.held"
        script = (
            "import time\n"
            "from pathlib import Path\n"
            "from portunus.filelock import flock_path\n"
            f"with flock_path(Path({str(lock_path)!r})):\n"
            f"    Path({str(ready)!r}).write_text('1')\n"
            "    time.sleep(30)\n"
        )
        p = subprocess.Popen([sys.executable, "-c", script], env=os.environ.copy())
        procs.append(p)
        deadline = time.monotonic() + 10
        while not ready.exists():
            assert time.monotonic() < deadline, "holder never acquired the lock"
            time.sleep(0.01)

    yield hold
    for p in procs:
        p.kill()
        p.wait()


def test_timeout_under_contention_raises_clear_error_without_hanging(tmp_path, held_lock):
    lock_path = tmp_path / "views.lock"
    held_lock(lock_path)

    start = time.monotonic()
    with pytest.raises(LockTimeout) as exc_info:
        with flock_path(lock_path, timeout=0.3):
            pytest.fail("acquired a lock another process holds")
    elapsed = time.monotonic() - start

    assert elapsed < 2.0, f"timeout of 0.3s took {elapsed:.2f}s -- lock wait did not stay bounded"
    assert isinstance(exc_info.value, TimeoutError)
    assert "0.3s" in str(exc_info.value)
    assert str(lock_path) in str(exc_info.value)


def test_lock_is_reacquirable_after_a_timeout(tmp_path, held_lock):
    """A timed-out attempt must not leak a held lock or fd."""
    contended = tmp_path / "a.lock"
    held_lock(contended)
    with pytest.raises(LockTimeout):
        with flock_path(contended, timeout=0.1):
            pass

    free = tmp_path / "b.lock"
    with flock_path(free, timeout=0.1):
        pass
    with flock_path(free, timeout=0.1):
        pass


def test_registry_maps_lock_timeout_to_registry_locked(home, held_lock):
    held_lock(home / "registry.lock")
    reg = Registry(lock_timeout=0.2)

    with pytest.raises(RegistryLocked, match="registry lock"):
        reg.add("x", "sm-x")
