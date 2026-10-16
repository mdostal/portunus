"""Credential liveness + cross-system parity primitives (GH #141).

The value reaches only the outbound request header; results, the persisted
status file, CLI/MCP output and the audit log carry status codes and
metadata only."""
import io
import json
import stat
import urllib.error

import pytest

from portunus import liveness
from portunus.audit import AuditChain
from portunus.backend import BackendError, MockBackend
from portunus.cli import main

SECRET = "sk-live-do-not-leak-0xBEEF"


class FakeTransport:
    def __init__(self, code=200, exc=None):
        self.code = code
        self.exc = exc
        self.calls = []

    def __call__(self, method, url, headers, timeout):
        self.calls.append((method, url, dict(headers), timeout))
        if self.exc is not None:
            raise self.exc
        return self.code


PROBE = liveness.HTTPProbe(url="https://api.example.test/v1/me")


@pytest.fixture
def fake_provider(monkeypatch):
    """Register a liveness adapter for provider 'example' with a fake
    transport, restoring the adapter table afterwards."""
    monkeypatch.setattr(liveness, "_ADAPTERS", {})
    transport = FakeTransport()
    liveness.register_liveness_adapter(
        "example", lambda: liveness.HTTPLivenessAdapter("example", PROBE, transport=transport))
    return transport


def _add(stack, name="example-key", provider="example", tags=None):
    stack["registry"].add(name, f"sm-{name}", provider=provider, tags=tags)
    stack["backend"].set(f"sm-{name}", SECRET)
    return stack["registry"].get(name)


# --- probe classification ------------------------------------------------

@pytest.mark.parametrize("code,expected", [
    (200, "live"), (204, "live"), (401, "dead"), (403, "dead"),
    (404, "unknown"), (429, "unknown"), (500, "unknown"),
])
def test_probe_classifies_status_codes(code, expected):
    assert PROBE.classify(code) == expected


def test_probe_header_forms():
    assert PROBE.headers_for("v") == {"Authorization": "Bearer v"}
    raw = liveness.HTTPProbe(url="u", header="X-Api-Key", scheme="")
    assert raw.headers_for("v") == {"X-Api-Key": "v"}


# --- adapter + check_reference --------------------------------------------

def test_live_check_sends_value_only_in_header(stack, fake_provider):
    ref = _add(stack)
    result = liveness.check_reference(ref, stack["resolver"])
    assert result.status == "live"
    assert result.http_status == 200
    method, url, headers, _ = fake_provider.calls[0]
    assert (method, url) == ("GET", PROBE.url)
    assert headers == {"Authorization": f"Bearer {SECRET}"}
    assert SECRET not in json.dumps(result.to_dict())


def test_dead_key_is_reported_dead(stack, fake_provider):
    fake_provider.code = 401
    result = liveness.check_reference(_add(stack), stack["resolver"])
    assert (result.status, result.http_status) == ("dead", 401)


def test_network_failure_is_unknown_not_dead(stack, fake_provider):
    fake_provider.exc = liveness.LivenessError("request failed: timeout")
    result = liveness.check_reference(_add(stack), stack["resolver"])
    assert result.status == "unknown"
    assert result.http_status is None


def test_gated_reference_is_unknown_and_never_probed(stack, fake_provider):
    ref = _add(stack)
    stack["broker"].gate(ref.name)
    result = liveness.check_reference(stack["registry"].get(ref.name), stack["resolver"])
    assert result.status == "unknown"
    assert "ApprovalRequired" in result.detail
    assert fake_provider.calls == []


def test_missing_backend_value_is_unknown(stack, fake_provider):
    stack["registry"].add("orphan", "sm-orphan", provider="example")
    result = liveness.check_reference(stack["registry"].get("orphan"), stack["resolver"])
    assert result.status == "unknown"
    assert "BackendError" in result.detail


def test_no_adapter_is_unknown(stack, fake_provider):
    ref = _add(stack, provider="nobody")
    result = liveness.check_reference(ref, stack["resolver"])
    assert result.status == "unknown"
    assert "no liveness adapter" in result.detail


def test_provider_tag_overrides_ref_provider(stack, fake_provider):
    ref = _add(stack, provider="gcp", tags={"liveness_provider": "example"})
    assert liveness.provider_for(ref) == "example"
    assert liveness.check_reference(ref, stack["resolver"]).status == "live"


def test_checkable_references_skips_dropped_and_unadapted(stack, fake_provider):
    _add(stack, "a")
    _add(stack, "b", provider="nobody")
    _add(stack, "c")
    stack["registry"].set_state("c", "dropped")
    assert [r.name for r in liveness.checkable_references(stack["registry"])] == ["a"]


# --- default transport -----------------------------------------------------

def test_default_transport_returns_code_and_never_reads_error_body(monkeypatch):
    body = io.BytesIO(b'{"message":"Invalid secret key: ' + SECRET.encode() + b'"}')
    err = urllib.error.HTTPError(PROBE.url, 401, "Unauthorized", {}, body)

    def fake_urlopen(req, timeout):
        raise err

    monkeypatch.setattr(liveness.urllib.request, "urlopen", fake_urlopen)
    assert liveness._default_transport("GET", PROBE.url, {}, 1.0) == 401
    assert body.closed or body.tell() == 0


def test_default_transport_network_error_has_no_detail_leak(monkeypatch):
    def fake_urlopen(req, timeout):
        raise urllib.error.URLError(TimeoutError(f"boom {SECRET}"))

    monkeypatch.setattr(liveness.urllib.request, "urlopen", fake_urlopen)
    with pytest.raises(liveness.LivenessError) as info:
        liveness._default_transport("GET", PROBE.url, {}, 1.0)
    assert SECRET not in str(info.value)


# --- persisted status / escalation clock ----------------------------------

def _result(status, at, code=None):
    return liveness.LivenessResult("k", "example", status, http_status=code, checked_at=at)


def test_first_failed_at_persists_until_live(home):
    liveness.record_results([_result("dead", 100, 401)])
    liveness.record_results([_result("dead", 200, 401)])
    st = liveness.load_liveness_status()["k"]
    assert (st.first_failed_at, st.consecutive_failures, st.last_checked_at) == (100, 2, 200)

    liveness.record_results([_result("live", 300, 200)])
    st = liveness.load_liveness_status()["k"]
    assert (st.first_failed_at, st.consecutive_failures, st.last_live_at) == (None, 0, 300)


def test_status_file_is_private(home):
    liveness.record_results([_result("live", 1, 200)])
    mode = stat.S_IMODE((home / "liveness-status.json").stat().st_mode)
    assert mode == 0o600


def test_failing_sorts_oldest_first(home):
    liveness.record_results([
        liveness.LivenessResult("new", "p", "dead", checked_at=500),
        liveness.LivenessResult("old", "p", "unknown", checked_at=100),
        liveness.LivenessResult("ok", "p", "live", checked_at=50),
    ])
    assert [s.ref_name for s in liveness.failing(liveness.load_liveness_status())] == ["old", "new"]


# --- parity ------------------------------------------------------------------

def test_parity_in_sync():
    result = liveness.parity_check("k", {"gcp": lambda: SECRET, "convex": lambda: SECRET})
    assert result.in_sync
    assert result.groups == (("gcp", "convex"),)


def test_parity_detects_drift_without_exposing_values():
    result = liveness.parity_check("k", {
        "gcp": lambda: "stale-" + SECRET,
        "convex": lambda: SECRET,
        "vercel": lambda: SECRET,
    })
    assert not result.in_sync
    assert result.groups == (("convex", "vercel"), ("gcp",))
    dumped = json.dumps(result.to_dict())
    assert SECRET not in dumped and "stale" not in dumped


def test_parity_reports_unreadable_target():
    def boom():
        raise BackendError(f"denied {SECRET}")

    result = liveness.parity_check("k", {"gcp": lambda: SECRET, "convex": boom})
    assert not result.in_sync
    assert result.errors == {"convex": "BackendError"}
    assert SECRET not in json.dumps(result.to_dict())


def test_backend_fetcher():
    fetch = liveness.backend_fetcher(MockBackend({"sm": SECRET}), "sm")
    assert liveness.parity_check("k", {"a": fetch, "b": lambda: SECRET}).in_sync


# --- CLI ---------------------------------------------------------------------

@pytest.fixture
def cli_mock(monkeypatch, fake_provider):
    monkeypatch.setenv("PORTUNUS_BACKEND", "mock")
    monkeypatch.setenv("PORTUNUS_MOCK_SM_EXAMPLE_KEY", SECRET)
    return fake_provider


def _register_cli():
    from portunus import Registry
    Registry().add("example-key", "sm-example-key", provider="example")


def test_cli_check_live_exit_zero_and_audited(home, capsys, cli_mock):
    _register_cli()
    rc = main(["liveness", "check", "example-key", "--json"])
    out = capsys.readouterr()
    assert rc == 0
    assert json.loads(out.out)["results"][0]["status"] == "live"
    assert SECRET not in out.out + out.err
    entries = [e for e in AuditChain().entries() if e["action"] == "liveness"]
    assert [(e["secret"], e["result"]) for e in entries] == [("sm-example-key", "live:200")]
    assert SECRET not in (home / "audit.log").read_text()


def test_cli_check_dead_exit_two_and_status_failing(home, capsys, cli_mock):
    _register_cli()
    cli_mock.code = 403
    assert main(["liveness", "check", "--all"]) == 2
    capsys.readouterr()
    assert main(["liveness", "status", "--failing", "--json"]) == 0
    rows = json.loads(capsys.readouterr().out)["statuses"]
    assert [(r["ref_name"], r["status"], r["http_status"]) for r in rows] == [("example-key", "dead", 403)]


def test_cli_check_unknown_exit_one(home, capsys, cli_mock):
    _register_cli()
    cli_mock.code = 503
    assert main(["liveness", "check", "example-key"]) == 1


def test_cli_check_requires_names_or_all(home, capsys, cli_mock):
    assert main(["liveness", "check"]) == 1
    assert main(["liveness", "check", "nope"]) == 1


def test_cli_status_empty(home, capsys):
    assert main(["liveness", "status"]) == 0
    assert "no liveness checks recorded" in capsys.readouterr().out


# --- MCP ---------------------------------------------------------------------

def test_mcp_liveness_tools_registered():
    from portunus import mcp_server
    names = {t.name for t in mcp_server.mcp._tool_manager.list_tools()}
    assert {"portunus_liveness_check", "portunus_liveness_status"} <= names


def test_mcp_liveness_check_and_status(home, cli_mock):
    from portunus import mcp_server
    _register_cli()
    cli_mock.code = 401
    result = mcp_server.portunus_liveness_check(names=["example-key"])
    assert result["results"][0]["status"] == "dead"
    assert SECRET not in json.dumps(result)
    status = mcp_server.portunus_liveness_status(failing=True)
    assert status["statuses"][0]["ref_name"] == "example-key"
