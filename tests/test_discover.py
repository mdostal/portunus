"""Read-only GCP Secret Manager discovery (story 04). Structurally incapable of
a value fetch: discover.py never imports/calls any SecretBackend.access()."""
import ast
import json
from pathlib import Path
from types import SimpleNamespace

import pytest

from portunus import Registry
from portunus.discover import (
    DiscoverError,
    derive_local_name,
    list_gcp_secrets,
    register_discovered,
)


def _mock_runner(stdout_json):
    def runner(cmd, capture_output, text, timeout):
        return SimpleNamespace(returncode=0, stdout=json.dumps(stdout_json), stderr="")
    return runner


def test_discover_module_never_imports_a_value_fetching_method():
    src = Path("src/portunus/discover.py").read_text()
    tree = ast.parse(src)
    names = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.ImportFrom):
            for alias in node.names:
                names.add(alias.name)
        if isinstance(node, ast.Attribute):
            names.add(node.attr)
    assert "GcloudBackend" not in names
    assert "access" not in names


def test_list_gcp_secrets_parses_names_labels_create_time():
    runner = _mock_runner([
        {
            "name": "projects/123/secrets/API_KEY",
            "labels": {"purpose": "stripe billing key"},
            "createTime": "2026-01-01T00:00:00Z",
        },
        {"name": "projects/123/secrets/NO_LABELS", "createTime": "2026-01-02T00:00:00Z"},
    ])
    secrets = list_gcp_secrets("demo-project", runner=runner)
    assert secrets[0].sm_name == "API_KEY"
    assert secrets[0].labels == {"purpose": "stripe billing key"}
    assert secrets[1].sm_name == "NO_LABELS"
    assert secrets[1].labels == {}


def test_list_gcp_secrets_never_calls_versions_access(monkeypatch):
    seen_cmds = []

    def runner(cmd, capture_output, text, timeout):
        seen_cmds.append(cmd)
        return SimpleNamespace(returncode=0, stdout="[]", stderr="")

    list_gcp_secrets("demo-project", runner=runner)
    for cmd in seen_cmds:
        assert "versions" not in cmd
        assert "access" not in cmd


def test_list_gcp_secrets_passes_account_flag_when_given():
    seen_cmds = []

    def runner(cmd, capture_output, text, timeout):
        seen_cmds.append(cmd)
        return SimpleNamespace(returncode=0, stdout="[]", stderr="")

    list_gcp_secrets("demo-project", account="user@example.com", runner=runner)
    assert "--account=user@example.com" in seen_cmds[0]


def test_list_gcp_secrets_no_account_flag_when_omitted():
    seen_cmds = []

    def runner(cmd, capture_output, text, timeout):
        seen_cmds.append(cmd)
        return SimpleNamespace(returncode=0, stdout="[]", stderr="")

    list_gcp_secrets("demo-project", runner=runner)
    assert not any(arg.startswith("--account=") for arg in seen_cmds[0])


def test_derive_local_name_prefixes_by_project():
    assert derive_local_name("demo-project", "API_KEY") == "demo-project-api_key"


def test_register_discovered_writes_requested_state_with_seeded_description(home):
    reg = Registry()
    from portunus.discover import DiscoveredSecret
    discovered = [DiscoveredSecret(sm_name="API_KEY", labels={"purpose": "stripe billing key"})]

    report = register_discovered(reg, "demo-project", discovered)

    assert report.registered == ["demo-project-api_key"]
    ref = reg.require("demo-project-api_key")
    assert ref.state == "requested"
    assert ref.sm_name == "API_KEY"
    assert ref.provider == "gcp"
    assert ref.project == "demo-project"
    assert ref.description == "stripe billing key"


def test_register_discovered_is_diff_only_without_writing(home):
    reg = Registry()
    from portunus.discover import DiscoveredSecret
    discovered = [DiscoveredSecret(sm_name="API_KEY", labels={})]

    already, not_yet = __import__("portunus.discover", fromlist=["diff_against_registry"]).diff_against_registry(
        reg, "demo-project", discovered
    )
    assert already == []
    assert not_yet == discovered
    assert "demo-project-api_key" not in reg


def test_register_discovered_never_overwrites_a_naming_collision(home):
    reg = Registry()
    # A pre-existing, unrelated reference that happens to derive the same local name.
    reg.add("demo-project-api_key", "SOME_OTHER_SM_NAME", provider="gcp", project="demo-project")
    from portunus.discover import DiscoveredSecret
    discovered = [DiscoveredSecret(sm_name="API_KEY", labels={})]

    report = register_discovered(reg, "demo-project", discovered)

    assert report.registered == []
    assert report.conflicts == ["demo-project-api_key"]
    unchanged = reg.require("demo-project-api_key")
    assert unchanged.sm_name == "SOME_OTHER_SM_NAME"


def test_register_discovered_skips_already_registered(home):
    reg = Registry()
    reg.add("demo-project-api_key", "API_KEY", provider="gcp", project="demo-project")
    from portunus.discover import DiscoveredSecret
    discovered = [DiscoveredSecret(sm_name="API_KEY", labels={})]

    report = register_discovered(reg, "demo-project", discovered)
    assert report.registered == []
    assert report.conflicts == []
    assert "demo-project-api_key" in report.already_registered


def test_check_injectable_fails_closed_for_discovered_references(home):
    from portunus import Broker, AuditChain
    reg = Registry()
    from portunus.discover import DiscoveredSecret
    register_discovered(reg, "demo-project", [DiscoveredSecret(sm_name="API_KEY", labels={})])
    broker = Broker(reg, AuditChain())
    with pytest.raises(Exception):
        broker.check_injectable("demo-project-api_key")
