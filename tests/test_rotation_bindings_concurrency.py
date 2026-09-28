"""rotation-bindings.json read-modify-write under concurrent writers.

The CLI, MCP server and UI can all write rotation bindings at once. Before
update_rotation_bindings() existed, `rotation-bindings set` did an unlocked
load -> modify -> save, so two writers could each load the same snapshot and
the second save silently dropped the first one's provider. Real separate OS
processes (fcntl.flock is per-process), same technique as test_backup.py.
"""
import os
import subprocess
import sys

from portunus.rotation import (
    RotationBinding,
    load_rotation_bindings,
    update_rotation_bindings,
)

N_PER_PROCESS = 40


def test_concurrent_cli_binding_adds_lose_no_entries(home):
    go = home / "go"
    script = (
        "import sys, time\n"
        "from pathlib import Path\n"
        "from portunus.cli import main\n"
        "worker = sys.argv[1]\n"
        f"go = Path({str(go)!r})\n"
        "deadline = time.monotonic() + 10\n"
        "while not go.exists() and time.monotonic() < deadline:\n"
        "    pass\n"
        f"for i in range({N_PER_PROCESS}):\n"
        "    rc = main(['rotation-bindings', 'set', f'{worker}-{i}', '--account', worker])\n"
        "    assert rc == 0\n"
    )
    procs = [
        subprocess.Popen(
            [sys.executable, "-c", script, worker],
            env=os.environ.copy(), stdout=subprocess.DEVNULL,
        )
        for worker in ("a", "b")
    ]
    go.write_text("1")  # release both workers at once to maximize overlap
    for p in procs:
        assert p.wait(timeout=60) == 0

    bindings = load_rotation_bindings()
    expected = {f"{w}-{i}" for w in ("a", "b") for i in range(N_PER_PROCESS)}
    missing = expected - set(bindings)
    assert not missing, f"lost {len(missing)} concurrent binding write(s): {sorted(missing)[:5]}"
    assert bindings["a-0"].account == "a"
    assert bindings["b-0"].account == "b"


def test_rotation_bindings_file_stays_0600(home):
    update_rotation_bindings(lambda b: b.__setitem__("gcp", RotationBinding("gcp")))
    assert oct((home / "rotation-bindings.json").stat().st_mode & 0o777) == "0o600"
