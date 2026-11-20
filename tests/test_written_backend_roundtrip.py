"""portunus#242 / PANT-1218: a reference resolves from the backend it was
actually written to, until it is explicitly synced or migrated.

The reported failure: `portunus_prompt` stored a new ref into the local vault
(stored: true, state: enabled) for project `ffe-cicd`, whose VaultBinding is
GCP. `portunus_resolve_to_tempfile` on the same name then followed the
project binding into gcloud and failed with a reauth error -- so the secret a
human just typed could not be read back by the agent that asked for it.

Every test here binds the project to GCP and makes any gcloud call fail the
test outright, so a pass proves resolve never touched gcloud."""
import json
import os
import subprocess

import pytest

from portunus import Registry, mcp_server
from portunus import prompt as prompt_mod
from portunus.backend import VaultBinding, save_vault_bindings
from portunus.cli import main

PROJECT = "ffe-cicd"
NAME = "ffe-cicd-autumn-prod-webhook-signing-secret"
SM_NAME = "autumn-prod-webhook-signing-secret"
GROUP = "ffe-cicd/social-engine/prod/autumn"
SECRET = "whsec-roundtrip-0xBEEF"


@pytest.fixture(autouse=True)
def _gcp_bound_project(home, monkeypatch, gcloud_on_path):
    monkeypatch.delenv("PORTUNUS_BACKEND", raising=False)

    def gcloud_must_not_run(*args, **kwargs):
        raise AssertionError(f"gcloud was invoked: {args!r}")

    monkeypatch.setattr("portunus.backend.subprocess.run", gcloud_must_not_run)
    save_vault_bindings({PROJECT: VaultBinding(PROJECT, backend="gcp")})


@pytest.fixture(params=["direct", "cached"])
def binding_mode(request):
    save_vault_bindings({PROJECT: VaultBinding(PROJECT, backend="gcp", sync_mode=request.param)})
    return request.param


def _dialog_answers(monkeypatch, value):
    def fake_run(argv, **kwargs):
        return subprocess.CompletedProcess(argv, 0, stdout=value + "\n", stderr="")

    monkeypatch.setattr(prompt_mod.subprocess, "run", fake_run)
    monkeypatch.setattr(prompt_mod, "available_surfaces", lambda **kw: ["osascript"])


def _resolve_back(name=NAME):
    result = mcp_server.portunus_resolve_to_tempfile(name=name)
    assert "error" not in result, result
    try:
        with open(result["path"]) as fh:
            return fh.read()
    finally:
        os.unlink(result["path"])


def test_mcp_prompt_then_resolve_round_trips_without_gcloud(monkeypatch, binding_mode):
    """The exact repro from portunus#242."""
    _dialog_answers(monkeypatch, SECRET)
    result = mcp_server.portunus_prompt(
        name=NAME, sm_name=SM_NAME, project=PROJECT, group=GROUP,
        enable=True, surface="osascript",
    )
    assert result["stored"] is True and result["state"] == "enabled"
    assert result["backend"] == "local"
    assert _resolve_back() == SECRET


def test_cli_prompt_then_resolve_round_trips_without_gcloud(monkeypatch, capsys):
    _dialog_answers(monkeypatch, SECRET)
    rc = main(["prompt", NAME, SM_NAME, "--project", PROJECT, "--group", GROUP, "--enable"])
    assert rc == 0
    assert json.loads(capsys.readouterr().out)["backend"] == "local"
    assert _resolve_back() == SECRET


def test_mcp_drop_then_resolve_round_trips_without_gcloud(binding_mode):
    result = mcp_server.portunus_drop(name=NAME, sm_name=SM_NAME, value=SECRET, project=PROJECT)
    assert result["backend"] == "local"
    mcp_server.portunus_state(NAME, "enabled")
    assert _resolve_back() == SECRET


def test_cli_drop_then_resolve_round_trips_without_gcloud(home, capsys):
    value_file = home / "value.txt"
    value_file.write_text(SECRET + "\n")
    assert main(["drop", NAME, SM_NAME, "--project", PROJECT, "--value-file", str(value_file)]) == 0
    assert "backend=local" in capsys.readouterr().out
    assert main(["state", NAME, "enabled"]) == 0
    assert _resolve_back() == SECRET


def test_drop_bulk_entries_round_trip_without_gcloud(home):
    result = mcp_server.portunus_drop_bulk([
        {"name": "a", "sm_name": "sm-a", "value": "VA", "project": PROJECT},
        {"name": "b", "sm_name": "sm-b", "value": "VB", "project": PROJECT},
    ])
    assert result["created"] == ["a", "b"]
    for name, want in (("a", "VA"), ("b", "VB")):
        mcp_server.portunus_state(name, "enabled")
        assert _resolve_back(name) == want

    entries = home / "entries.json"
    entries.write_text(json.dumps([{"name": "c", "sm_name": "sm-c", "value": "VC", "project": PROJECT}]))
    assert main(["drop-bulk", str(entries)]) == 0
    main(["state", "c", "enabled"])
    assert _resolve_back("c") == "VC"


def test_written_backend_is_visible_in_metadata_without_a_resolve(capsys):
    """A human can tell where a ref went from list/search/show -- no resolve,
    no backend access."""
    mcp_server.portunus_drop(name=NAME, sm_name=SM_NAME, value=SECRET, project=PROJECT)

    assert Registry().require(NAME).backend == "local"
    (listed,) = mcp_server.portunus_list(PROJECT)
    assert listed["backend"] == "local"
    (found,) = mcp_server.portunus_search("webhook", project=PROJECT)
    assert found["backend"] == "local"

    assert main(["list", "--project", PROJECT]) == 0
    assert "backend:     local" in capsys.readouterr().out
    assert main(["list", "--project", PROJECT, "--json"]) == 0
    assert json.loads(capsys.readouterr().out)[0]["backend"] == "local"


def test_explicit_backend_override_is_still_honored():
    """An explicit `backend` on drop is the deliberate "route this elsewhere"
    step -- it is recorded as given, not replaced by the write location."""
    result = mcp_server.portunus_drop(
        name=NAME, sm_name=SM_NAME, value=SECRET, project=PROJECT, backend="gcp",
    )
    assert result["backend"] == "gcp"
    assert Registry().require(NAME).backend == "gcp"


def test_retag_backend_repins_a_ref_written_before_the_fix(capsys):
    """Refs dropped before this fix have backend="" and still follow the GCP
    binding; `retag --backend local` pins them without re-entering a value."""
    from portunus.localvault import LocalEncryptedBackend

    Registry().add(NAME, SM_NAME, project=PROJECT, state="enabled")
    LocalEncryptedBackend().store(SM_NAME, SECRET)

    assert main(["retag", NAME, "--backend", "local"]) == 0
    assert Registry().require(NAME).backend == "local"
    assert _resolve_back() == SECRET

    assert main(["retag", NAME, "--backend", "default"]) == 0
    assert Registry().require(NAME).backend == ""
