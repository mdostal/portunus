"""PANT-1147 / GH #164: Bitwarden adapter -- metadata-only discovery via the
`bw` CLI, BitwardenBackend resolution, router dispatch, register-as-requested,
CLI + MCP entry points. `bw` is never actually executed: every test injects a
fake runner (or monkeypatches subprocess.run + shutil.which)."""
import json
import subprocess
from types import SimpleNamespace

import pytest

from portunus import bitwarden
from portunus.audit import AuditChain
from portunus.backend import BackendError, BitwardenBackend

SECRET = "hunter2-SUPER-SECRET"
TOTP_SEED = "JBSWY3DPEHPK3PXP"

ITEMS = [
    {
        "id": "11111111-aaaa-bbbb-cccc-000000000001",
        "type": 1,
        "name": "Instagram",
        "folderId": None,
        "organizationId": None,
        "notes": "recovery codes: 1234 5678",
        "revisionDate": "2026-10-01T00:00:00Z",
        "login": {
            "username": "brand@example.com",
            "password": SECRET,
            "totp": TOTP_SEED,
            "uris": [{"match": None, "uri": "https://instagram.com"}],
        },
        "fields": [{"name": "api_key", "value": "custom-SECRET-value", "type": 1}],
    },
    {
        "id": "22222222-aaaa-bbbb-cccc-000000000002",
        "type": 2,
        "name": "Wifi note",
        "notes": "wifi pw: SECRET-wifi",
        "secureNote": {"type": 0},
    },
]


class FakeBw:
    """Records argv/env and answers like the real `bw`."""

    def __init__(self, items=None, returncode=0, stderr=""):
        self.items = ITEMS if items is None else items
        self.returncode = returncode
        self.stderr = stderr
        self.calls = []

    def __call__(self, cmd, **kwargs):
        self.calls.append((cmd, kwargs))
        if self.returncode:
            return subprocess.CompletedProcess(cmd, self.returncode, "", self.stderr)
        args = [a for a in cmd[1:] if a != "--nointeraction"]
        out = ""
        if args[:2] == ["list", "items"]:
            q = args[3].lower() if len(args) > 3 else ""
            out = json.dumps([i for i in self.items if q in i["name"].lower()])
        elif args[0] == "status":
            out = json.dumps({"status": "unlocked", "userEmail": "me@example.com",
                              "serverUrl": "https://vault.bitwarden.com", "lastSync": "x"})
        elif args[0] == "get":
            item = next((i for i in self.items if i["id"] == args[2]), None)
            if item is None:
                return subprocess.CompletedProcess(cmd, 1, "", "Not found.")
            if args[1] == "item":
                out = json.dumps(item)
            elif args[1] == "notes":
                out = item.get("notes", "")
            else:
                out = item["login"][args[1]] + "\n"
        return subprocess.CompletedProcess(cmd, 0, out, "")


# --- discovery: metadata only -------------------------------------------------

def test_list_items_projects_away_every_value():
    items = bitwarden.list_items(runner=FakeBw())
    dumped = json.dumps([i.to_dict() for i in items])
    assert "SECRET" not in dumped and TOTP_SEED not in dumped and "recovery" not in dumped
    ig = items[0]
    assert ig.name == "Instagram"
    assert ig.username == "brand@example.com"
    assert ig.uris == ["https://instagram.com"]
    assert ig.custom_fields == ["api_key"]
    assert ig.type == "login"
    assert items[1].type == "secure_note"


def test_list_items_passes_search_and_never_puts_session_on_argv(monkeypatch):
    monkeypatch.setenv("BW_SESSION", "session-token-xyz")
    fake = FakeBw()
    items = bitwarden.list_items("insta", runner=fake)
    assert [i.name for i in items] == ["Instagram"]
    cmd, kwargs = fake.calls[0]
    assert cmd[:5] == ["bw", "list", "items", "--search", "insta"]
    assert "--nointeraction" in cmd
    assert "session-token-xyz" not in " ".join(cmd)
    assert kwargs["env"]["BW_SESSION"] == "session-token-xyz"


def test_locked_vault_fails_closed_with_remediation():
    fake = FakeBw(returncode=1, stderr="Vault is locked.")
    with pytest.raises(bitwarden.BitwardenError, match="BW_SESSION"):
        bitwarden.list_items(runner=fake)


def test_missing_bw_cli(monkeypatch):
    monkeypatch.setattr(bitwarden.shutil, "which", lambda _c: None)
    with pytest.raises(bitwarden.BitwardenError, match="not found on PATH"):
        bitwarden.list_items()


def test_malformed_json():
    def runner(cmd, **kw):
        return subprocess.CompletedProcess(cmd, 0, "not json", "")
    with pytest.raises(bitwarden.BitwardenError, match="malformed"):
        bitwarden.list_items(runner=runner)


def test_status():
    st = bitwarden.status(runner=FakeBw())
    assert st["status"] == "unlocked" and st["userEmail"] == "me@example.com"


# --- sm_name ------------------------------------------------------------------

@pytest.mark.parametrize("sm,expected", [
    ("bitwarden:abc", ("abc", "password")),
    ("bitwarden:abc:username", ("abc", "username")),
    ("bitwarden:abc:my:field", ("abc", "my:field")),
])
def test_parse_sm_name(sm, expected):
    assert bitwarden.parse_sm_name(sm) == expected


@pytest.mark.parametrize("bad", ["abc", "bitwarden:", "chrome:x:y:z"])
def test_parse_sm_name_rejects(bad):
    with pytest.raises(bitwarden.BitwardenError):
        bitwarden.parse_sm_name(bad)


def test_make_sm_name_round_trips():
    assert bitwarden.make_sm_name("abc") == "bitwarden:abc"
    assert bitwarden.parse_sm_name(bitwarden.make_sm_name("abc", "api_key")) == ("abc", "api_key")


# --- backend ------------------------------------------------------------------

def test_backend_resolves_password_by_default():
    be = BitwardenBackend(audit=AuditChain(), runner=FakeBw())
    assert be.access("bitwarden:11111111-aaaa-bbbb-cccc-000000000001") == SECRET


def test_backend_resolves_username_and_custom_field():
    be = BitwardenBackend(audit=AuditChain(), runner=FakeBw())
    iid = "11111111-aaaa-bbbb-cccc-000000000001"
    assert be.access(f"bitwarden:{iid}:username") == "brand@example.com"
    assert be.access(f"bitwarden:{iid}:api_key") == "custom-SECRET-value"


def test_backend_errors_are_backend_errors():
    be = BitwardenBackend(audit=AuditChain(), runner=FakeBw())
    with pytest.raises(BackendError, match="not found"):
        be.access("bitwarden:nope")
    with pytest.raises(BackendError, match="no field named"):
        be.access("bitwarden:11111111-aaaa-bbbb-cccc-000000000001:missing")
    with pytest.raises(BackendError, match="sm_name must be"):
        be.access("garbage")


def test_router_dispatches_bitwarden_by_ref_backend_field():
    from portunus.cli import _make_backend_router

    router = _make_backend_router({}, AuditChain(), fallback_backend=SimpleNamespace())
    assert isinstance(router(SimpleNamespace(backend="bitwarden", project="")), BitwardenBackend)


# --- register -----------------------------------------------------------------

def test_register_items_as_requested_placeholders(home):
    from portunus.registry import Registry

    registry = Registry()
    items = bitwarden.list_items(runner=FakeBw())
    report = bitwarden.register_items(registry, items)
    assert report.registered == ["bw-instagram-11111111"]  # secure note skipped
    ref = registry.get("bw-instagram-11111111")
    assert ref.state == "requested"
    assert ref.backend == "bitwarden"
    assert ref.sm_name == "bitwarden:11111111-aaaa-bbbb-cccc-000000000001"
    assert ref.tags["username"] == "brand@example.com"

    again = bitwarden.register_items(Registry(), items)
    assert again.already_registered == ["bw-instagram-11111111"] and not again.registered


def test_register_reports_conflict_never_overwrites(home):
    from portunus.registry import Registry

    registry = Registry()
    registry.add("bw-instagram-11111111", "something-else")
    report = bitwarden.register_items(registry, bitwarden.list_items(runner=FakeBw()))
    assert report.conflicts == ["bw-instagram-11111111"]
    assert registry.get("bw-instagram-11111111").sm_name == "something-else"


def test_registered_item_is_found_by_search_and_fails_closed(home):
    from portunus.registry import Registry
    from portunus.search import search_references

    registry = Registry()
    bitwarden.register_items(registry, bitwarden.list_items(runner=FakeBw()))
    hits = search_references(Registry(), "instagram")
    assert [h.name for h in hits] == ["bw-instagram-11111111"]
    assert hits[0].state == "requested"  # Broker's allowlist refuses to inject this


# --- CLI + MCP ----------------------------------------------------------------

@pytest.fixture
def fake_bw_on_path(monkeypatch):
    fake = FakeBw()
    monkeypatch.setattr(bitwarden.shutil, "which", lambda c: "/usr/bin/bw" if c == "bw" else None)
    monkeypatch.setattr(bitwarden, "_default_runner", fake)
    return fake


def test_cli_list_json_and_register(home, fake_bw_on_path, capsys):
    from portunus.cli import main
    from portunus.registry import Registry

    assert main(["bitwarden", "list", "--search", "insta", "--register", "--json"]) == 0
    out = capsys.readouterr().out
    assert "SECRET" not in out and TOTP_SEED not in out
    data = json.loads(out)
    assert data["items"][0]["name"] == "Instagram"
    assert data["registered"] == ["bw-instagram-11111111"]
    assert Registry().get("bw-instagram-11111111").backend == "bitwarden"


def test_cli_status(home, fake_bw_on_path, capsys):
    from portunus.cli import main

    assert main(["bitwarden", "status"]) == 0
    assert "unlocked" in capsys.readouterr().out


def test_mcp_bitwarden_search(home, fake_bw_on_path):
    from portunus.mcp_server import portunus_bitwarden_search

    result = portunus_bitwarden_search("insta")
    assert "SECRET" not in json.dumps(result)
    assert [i["name"] for i in result["items"]] == ["Instagram"]
    assert "registered" not in result


def test_mcp_bitwarden_search_locked(home, monkeypatch):
    monkeypatch.setattr(bitwarden.shutil, "which", lambda c: "/usr/bin/bw")
    monkeypatch.setattr(bitwarden, "_default_runner", FakeBw(returncode=1, stderr="You are not logged in."))
    from portunus.mcp_server import portunus_bitwarden_search

    assert "BW_SESSION" in portunus_bitwarden_search("x")["error"]
