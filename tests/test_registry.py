"""Registry never stores a value; persists 0600; round-trips."""
import json
import os
import stat

import pytest

from portunus import Registry, Reference


def test_add_get_and_persist(home):
    reg = Registry()
    reg.add("shared-anthropic", "dostal-shared-anthropic", scope="shared",
            kind="anthropic", project="proj")
    # reload from disk
    reg2 = Registry()
    ref = reg2.require("shared-anthropic")
    assert ref.sm_name == "dostal-shared-anthropic"
    assert ref.sm_path == "projects/proj/secrets/dostal-shared-anthropic"
    assert ref.state == "enabled"


def test_reference_has_no_value_field():
    assert "value" not in Reference.__dataclass_fields__
    assert "secret" not in Reference.__dataclass_fields__


def test_persisted_file_is_0600_and_has_no_value(home):
    reg = Registry()
    reg.add("x", "dostal-x")
    mode = stat.S_IMODE(os.stat(reg.path).st_mode)
    assert mode == 0o600
    raw = json.loads(reg.path.read_text())
    assert "value" not in raw["x"]


def test_remove_and_contains(home):
    reg = Registry()
    reg.add("x", "dostal-x")
    assert "x" in reg
    assert reg.remove("x") is True
    assert "x" not in reg
    assert reg.remove("x") is False


def test_invalid_state_rejected(home):
    reg = Registry()
    with pytest.raises(ValueError):
        reg.add("x", "dostal-x", state="bogus")
