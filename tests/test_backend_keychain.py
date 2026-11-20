"""PANT-1403 / GH #254: OS keystore backend (`keychain:<service>[:<account>]`)
across macOS Keychain, Linux Secret Service and Windows Credential Manager.
No real keystore is touched: every test injects a fake runner / CredRead."""
import subprocess
from types import SimpleNamespace

import pytest

from portunus import keystore
from portunus.audit import AuditChain
from portunus.backend import BackendError, KeychainBackend

SECRET = "s3cret-VALUE"


class FakeRunner:
    def __init__(self, returncode=0, stdout="", stderr="", raise_timeout=False):
        self.returncode, self.stdout, self.stderr = returncode, stdout, stderr
        self.raise_timeout = raise_timeout
        self.calls = []

    def __call__(self, cmd, **kwargs):
        self.calls.append((cmd, kwargs))
        if self.raise_timeout:
            raise subprocess.TimeoutExpired(cmd, kwargs.get("timeout", 0))
        return subprocess.CompletedProcess(cmd, self.returncode, self.stdout, self.stderr)


# --- sm_name -------------------------------------------------------------

@pytest.mark.parametrize("sm_name,expected", [
    ("keychain:github", ("github", "")),
    ("keychain:github:alice", ("github", "alice")),
    ("keychain:https%3A//api.example.com:bob@example.com", ("https://api.example.com", "bob@example.com")),
    ("keychain:svc:acct:with:colons", ("svc", "acct:with:colons")),
])
def test_parse_sm_name(sm_name, expected):
    assert keystore.parse_sm_name(sm_name) == expected


@pytest.mark.parametrize("bad", ["github", "keychain:", "keychain::alice", "chrome:x"])
def test_parse_sm_name_rejects(bad):
    with pytest.raises(keystore.KeystoreError):
        keystore.parse_sm_name(bad)


@pytest.mark.parametrize("service,account", [
    ("github", ""), ("github", "alice"), ("https://api.example.com", "bob@example.com"),
])
def test_make_sm_name_roundtrips(service, account):
    assert keystore.parse_sm_name(keystore.make_sm_name(service, account)) == (service, account)


# --- macOS ---------------------------------------------------------------

def test_macos_reads_value_from_stdout_not_argv():
    run = FakeRunner(stdout=SECRET + "\n")
    be = KeychainBackend(audit=AuditChain(), runner=run, platform="darwin")
    assert be.access("keychain:github:alice") == SECRET
    cmd, kwargs = run.calls[0]
    assert cmd == ["security", "find-generic-password", "-s", "github", "-a", "alice", "-w"]
    assert kwargs["capture_output"] is True


def test_macos_without_account_omits_dash_a():
    run = FakeRunner(stdout=SECRET + "\n")
    KeychainBackend(runner=run, platform="darwin").access("keychain:github")
    assert "-a" not in run.calls[0][0]


def test_macos_not_found():
    run = FakeRunner(returncode=44, stderr="security: SecKeychainSearchCopyNext: The specified item could not be found in the keychain.")
    with pytest.raises(BackendError, match="no Keychain item"):
        KeychainBackend(runner=run, platform="darwin").access("keychain:nope")


def test_macos_locked_fails_closed():
    run = FakeRunner(returncode=36, stderr="security: SecKeychainItemCopyContent: User interaction is not allowed.")
    with pytest.raises(BackendError, match="locked"):
        KeychainBackend(runner=run, platform="darwin").access("keychain:github")


def test_timeout_is_reported_as_locked_prompt():
    run = FakeRunner(raise_timeout=True)
    with pytest.raises(BackendError, match="unlock prompt"):
        KeychainBackend(runner=run, platform="darwin").access("keychain:github")


# --- Linux ---------------------------------------------------------------

def test_linux_uses_keyring_compatible_attributes():
    run = FakeRunner(stdout=SECRET)
    assert KeychainBackend(runner=run, platform="linux").access("keychain:github:alice") == SECRET
    assert run.calls[0][0] == ["secret-tool", "lookup", "service", "github", "username", "alice"]


def test_linux_not_found_mentions_lock_ambiguity():
    run = FakeRunner(returncode=1)
    with pytest.raises(BackendError, match="no Secret Service item.*locked"):
        KeychainBackend(runner=run, platform="linux").access("keychain:nope")


def test_linux_no_session_bus_fails_closed():
    run = FakeRunner(returncode=1, stderr="secret-tool: Cannot autolaunch D-Bus without X11 $DISPLAY")
    with pytest.raises(BackendError, match="no Secret Service is reachable"):
        KeychainBackend(runner=run, platform="linux").access("keychain:github")


def test_linux_locked_collection():
    run = FakeRunner(returncode=1, stderr="secret-tool: Cannot get secret of a locked object")
    with pytest.raises(BackendError, match="collection is locked"):
        KeychainBackend(runner=run, platform="linux").access("keychain:github")


def test_missing_tool_without_runner(monkeypatch):
    monkeypatch.setattr(keystore.shutil, "which", lambda name: None)
    with pytest.raises(BackendError, match="secret-tool` not found"):
        KeychainBackend(platform="linux").access("keychain:github")


# --- Windows -------------------------------------------------------------

def test_windows_utf16_blob():
    be = KeychainBackend(platform="win32", win_reader=lambda t: ("alice", SECRET.encode("utf-16-le")))
    assert be.access("keychain:github:alice") == SECRET


def test_windows_utf8_blob():
    be = KeychainBackend(platform="win32", win_reader=lambda t: ("alice", b"abc"))
    assert be.access("keychain:github") == "abc"


def test_windows_account_mismatch_fails_closed():
    be = KeychainBackend(platform="win32", win_reader=lambda t: ("mallory", b"abcd"))
    with pytest.raises(BackendError, match="different user name"):
        be.access("keychain:github:alice")


def test_windows_not_found():
    be = KeychainBackend(platform="win32", win_reader=lambda t: None)
    with pytest.raises(BackendError, match="no Credential Manager entry"):
        be.access("keychain:github")


def test_unsupported_platform():
    with pytest.raises(BackendError, match="no supported OS keystore"):
        KeychainBackend(platform="sunos5").access("keychain:github")


# --- errors never leak values / wiring ----------------------------------

def test_error_messages_do_not_echo_stdout():
    run = FakeRunner(returncode=2, stdout=SECRET, stderr="weird failure")
    with pytest.raises(BackendError) as exc:
        KeychainBackend(runner=run, platform="darwin").access("keychain:github")
    assert SECRET not in str(exc.value)


def test_router_dispatches_keychain():
    from portunus.cli import _make_backend_router

    router = _make_backend_router({}, AuditChain(), fallback_backend=SimpleNamespace())
    assert isinstance(router(SimpleNamespace(backend="keychain", project="")), KeychainBackend)


def test_tool_status_platforms(monkeypatch):
    monkeypatch.setattr(keystore.shutil, "which", lambda name: "/usr/bin/" + name)
    assert keystore.tool_status("darwin")[0] is True
    assert keystore.tool_status("linux")[0] is True
    assert keystore.tool_status("win32")[0] is True
    assert keystore.tool_status("sunos5")[0] is False
    monkeypatch.setattr(keystore.shutil, "which", lambda name: None)
    ok, detail = keystore.tool_status("linux")
    assert not ok and "secret-tool" in detail
