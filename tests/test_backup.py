"""Coordinated multi-file snapshot (portunus-vault-backup story 02) --
foundation for `portunus vault export`/`import`. Real multi-process
regression tests (matching test_audit.py's own barrier-file technique),
not thread-based approximations -- fcntl.flock is a per-process construct,
so only separate OS processes exercise the real contention this exists to
serialize.
"""
import json
import os
import subprocess
import sys
import time

from portunus import AuditChain, Registry
from portunus.backend import VaultBinding, save_vault_bindings
from portunus.backup import LOCKED_FILES, UNLOCKED_FILES, snapshot
from portunus.filelock import flock_path


def test_snapshot_omits_absent_optional_files_without_erroring(home):
    Registry().add("x", "sm-x")
    AuditChain()  # touches audit.log into existence
    save_vault_bindings({"demo": VaultBinding("demo")})

    result = snapshot(home)

    assert "registry.json" in result
    assert "audit.log" in result
    assert "vault-bindings.json" in result
    # never created in this test -- must be omitted, not an error
    assert "master.key" not in result
    assert "vault.enc.json" not in result
    # .clock isn't touched until the first AuditChain.append() -- absent here
    assert ".clock" not in result
    assert "gcp-bindings.json" not in result
    assert "rotation-bindings.json" not in result


def test_snapshot_includes_clock_alongside_audit_log_once_present(home):
    """.clock (append()'s seq counter) must travel WITH audit.log, not just
    alongside it -- see backup.py's own LOCKED_FILES comment for why an
    import that restores audit.log without .clock would break the chain."""
    a = AuditChain()
    a.append("resolve", "sm-x", "ok")

    result = snapshot(home)

    assert ".clock" in result
    assert result[".clock"] == (home / ".clock").read_bytes()


def test_snapshot_includes_a_present_legacy_file_unlocked(home):
    (home / "gcp-bindings.json").write_text(json.dumps({"demo": {}}))
    Registry().add("x", "sm-x")

    result = snapshot(home)

    assert result["gcp-bindings.json"] == (home / "gcp-bindings.json").read_bytes()


def test_vault_bindings_lock_blocks_a_second_process_until_released(home):
    """Real regression-style proof: a second process attempting the SAME
    vault-bindings lock genuinely waits for the first to release, rather
    than acquiring concurrently."""
    lock_path = home / "vault-bindings.lock"
    a_acquired = home / "a-acquired.json"
    b_acquired = home / "b-acquired.json"
    hold_seconds = 1.0

    holder_script = (
        "import time, json\n"
        "from pathlib import Path\n"
        "from portunus.filelock import flock_path\n"
        f"lock = Path({str(lock_path)!r})\n"
        f"marker = Path({str(a_acquired)!r})\n"
        "with flock_path(lock):\n"
        "    marker.write_text(json.dumps({'t': time.time()}))\n"
        f"    time.sleep({hold_seconds})\n"
    )
    waiter_script = (
        "import time, json\n"
        "from pathlib import Path\n"
        "from portunus.filelock import flock_path\n"
        f"lock = Path({str(lock_path)!r})\n"
        f"a_marker = Path({str(a_acquired)!r})\n"
        f"b_marker = Path({str(b_acquired)!r})\n"
        "deadline = time.monotonic() + 5\n"
        "while not a_marker.exists() and time.monotonic() < deadline:\n"
        "    pass\n"  # wait for the holder to actually acquire first
        "with flock_path(lock):\n"
        "    b_marker.write_text(json.dumps({'t': time.time()}))\n"
    )

    holder = subprocess.Popen([sys.executable, "-c", holder_script], env=os.environ.copy())
    waiter = subprocess.Popen([sys.executable, "-c", waiter_script], env=os.environ.copy())
    assert holder.wait(timeout=10) == 0
    assert waiter.wait(timeout=10) == 0

    a_t = json.loads(a_acquired.read_text())["t"]
    b_t = json.loads(b_acquired.read_text())["t"]
    # The waiter can only have acquired the lock after the holder's
    # hold_seconds elapsed (minus a small tolerance for scheduling jitter).
    assert b_t - a_t >= hold_seconds - 0.2, (
        f"waiter acquired the lock too early (after {b_t - a_t:.3f}s, "
        f"expected >= {hold_seconds - 0.2:.3f}s) -- lock did not block"
    )


def test_coordinated_snapshot_never_observes_mismatched_generations(home):
    """A writer process repeatedly updates registry.json and
    vault-bindings.json to the SAME generation number, always under both
    locks held together, in the same fixed order snapshot() itself uses.
    A concurrent reader taking real coordinated snapshots must never catch
    the two files at different generations -- proof the multi-lock
    acquisition genuinely produces one consistent instant, not two
    independent reads straddling the writer's mutation."""
    (home / "registry.json").write_text(json.dumps({"gen": -1}))
    (home / "vault-bindings.json").write_text(json.dumps({"gen": -1}))
    stop_marker = home / "stop"

    writer_script = (
        "import json, time\n"
        "from pathlib import Path\n"
        "from portunus.filelock import flock_path\n"
        f"base = Path({str(home)!r})\n"
        f"stop = Path({str(stop_marker)!r})\n"
        "i = 0\n"
        "while not stop.exists():\n"
        "    with flock_path(base / 'registry.lock'):\n"
        "        with flock_path(base / 'vault-bindings.lock'):\n"
        "            (base / 'registry.json').write_text(json.dumps({'gen': i}))\n"
        "            (base / 'vault-bindings.json').write_text(json.dumps({'gen': i}))\n"
        "    i += 1\n"
    )
    writer = subprocess.Popen([sys.executable, "-c", writer_script], env=os.environ.copy())
    try:
        deadline = time.monotonic() + 3.0
        checks = 0
        while time.monotonic() < deadline:
            result = snapshot(home)
            reg_gen = json.loads(result["registry.json"])["gen"]
            bind_gen = json.loads(result["vault-bindings.json"])["gen"]
            assert reg_gen == bind_gen, (
                f"snapshot caught mismatched generations: registry={reg_gen} "
                f"vault-bindings={bind_gen}"
            )
            checks += 1
        assert checks > 5, "writer/reader race did not overlap enough to be a real test"
    finally:
        stop_marker.write_text("stop")
        writer.wait(timeout=10)


def test_concurrent_snapshot_calls_never_deadlock(home):
    """Real regression guard: N processes all calling snapshot() repeatedly
    and concurrently must all complete within a bounded time. A future
    change that acquires the four locks in an inconsistent order across
    call sites would risk a genuine deadlock here, not just in theory."""
    Registry().add("x", "sm-x")
    AuditChain()
    save_vault_bindings({"demo": VaultBinding("demo")})

    script = (
        "from portunus.backup import snapshot\n"
        "for _ in range(20):\n"
        "    snapshot()\n"
    )
    n = 6
    procs = [
        subprocess.Popen([sys.executable, "-c", script], env=os.environ.copy())
        for _ in range(n)
    ]
    for p in procs:
        assert p.wait(timeout=15) == 0


def test_locked_files_lock_acquisition_order_is_fixed_and_alphabetical():
    """The one and only acquisition order this primitive ever uses -- the
    design decision (design-discussion.md §3) that prevents lock-ordering
    deadlock against any future multi-lock caller."""
    order = sorted(set(LOCKED_FILES.values()))
    assert order == sorted(order)
    assert order == [".clock.lock", "registry.lock", "vault-bindings.lock", "vault.enc.lock"]
