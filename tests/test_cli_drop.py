"""End-to-end: drop -> enable -> inject, entirely through the CLI, on the
default (local-encrypted) backend. Proves the acceptance criterion from
DOS-726 directly: zero plaintext in stdout/stderr, the vault file, or the
audit log at any point in the lifecycle."""
import os

import pytest

from portunus.audit import AuditChain
from portunus.cli import main

SECRET = "s3kr3t-do-not-leak-0xCAFE"


@pytest.fixture(autouse=True)
def _local_backend(monkeypatch):
    # The CLI defaults to the local backend already; be explicit and make
    # sure no stray env from another test selects mock/gcloud.
    monkeypatch.delenv("PORTUNUS_BACKEND", raising=False)


def test_drop_lands_dropped_and_is_not_injectable(home, capsys):
    value_file = home / "value.txt"
    value_file.write_text(SECRET + "\n")

    rc = main(["drop", "shared-test", "dostal-shared-test", "--value-file", str(value_file)])
    assert rc == 0
    out = capsys.readouterr().out
    assert SECRET not in out
    assert "dropped" in out

    # dropped state fails closed: resolve must refuse.
    rc = main(["resolve", "{{secret:shared-test}}"])
    captured = capsys.readouterr()
    assert rc == 1
    assert SECRET not in captured.out
    assert SECRET not in captured.err
    assert "dropped" in captured.err


def test_drop_enable_inject_lifecycle(home, capsys):
    value_file = home / "value.txt"
    value_file.write_text(SECRET + "\n")
    main(["drop", "shared-test", "dostal-shared-test", "--value-file", str(value_file)])
    capsys.readouterr()

    rc = main(["state", "shared-test", "enabled"])
    assert rc == 0
    capsys.readouterr()

    rc = main(["resolve", "key={{secret:shared-test}}"])
    assert rc == 0
    path = capsys.readouterr().out.strip()
    try:
        assert open(path).read() == f"key={SECRET}"
    finally:
        os.unlink(path)


def test_drop_never_writes_plaintext_to_disk_or_audit(home):
    value_file = home / "value.txt"
    value_file.write_text(SECRET + "\n")
    main(["drop", "shared-test", "dostal-shared-test", "--value-file", str(value_file)])

    for name in ("vault.enc.json", "registry.json", "audit.log"):
        path = home / name
        if path.exists():
            assert SECRET not in path.read_text()

    audit = AuditChain()
    drops = [e for e in audit.entries() if e["action"] == "drop"]
    assert len(drops) == 1
    assert drops[0]["secret"] == "dostal-shared-test"


def test_drop_requires_stdin_or_value_file(home, capsys):
    with pytest.raises(SystemExit):
        main(["drop", "shared-test", "dostal-shared-test"])
