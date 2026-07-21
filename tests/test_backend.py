"""ARCA conformance suite — every registered backend behaves identically.

Never touches a real cloud: gcloud/aws are stubbed via monkeypatched
`shutil.which` + `subprocess.run`. Azure has no live implementation (EPIC
DOS-89) so it is exercised in "unimplemented" mode only.

OPAQUE_BYTES stands in for a real secret value everywhere in this file, so a
leaked assertion or stray print reads back as an obviously-fake marker
instead of a value that could be mistaken for a real credential.
"""
import subprocess

import pytest

from portunus.backend import (
    AwsBackend, AzureBackend, BackendError, GcloudBackend, MockBackend,
    SecretBackend, get_backend,
)

OPAQUE_BYTES = "OPAQUE-TEST-VALUE-DO-NOT-LOG"


def _fake_run_ok(stdout):
    def _run(cmd, capture_output, text, timeout):
        return subprocess.CompletedProcess(cmd, 0, stdout=stdout, stderr="")
    return _run


def _fake_run_fail(stderr="denied"):
    def _run(cmd, capture_output, text, timeout):
        return subprocess.CompletedProcess(cmd, 1, stdout="", stderr=stderr)
    return _run


# --- conformance: implemented providers behave identically -----------------

def _gcp_ready(monkeypatch, stdout=OPAQUE_BYTES):
    monkeypatch.setattr("shutil.which", lambda _: "/usr/bin/gcloud")
    monkeypatch.setattr(subprocess, "run", _fake_run_ok(stdout))
    return GcloudBackend()


def _aws_ready(monkeypatch, stdout=OPAQUE_BYTES):
    monkeypatch.setattr("shutil.which", lambda _: "/usr/local/bin/aws")
    monkeypatch.setattr(subprocess, "run", _fake_run_ok(stdout))
    return AwsBackend()


def _mock_ready(monkeypatch, stdout=OPAQUE_BYTES):
    b = MockBackend()
    b.set("dostal-x", stdout)
    return b


IMPLEMENTED = {
    "gcp": _gcp_ready,
    "aws": _aws_ready,
    "mock": _mock_ready,
}


@pytest.mark.parametrize("provider", sorted(IMPLEMENTED))
def test_isinstance_of_protocol(monkeypatch, provider):
    backend = IMPLEMENTED[provider](monkeypatch)
    assert isinstance(backend, SecretBackend)


@pytest.mark.parametrize("provider", sorted(IMPLEMENTED))
def test_access_returns_the_value(monkeypatch, provider):
    backend = IMPLEMENTED[provider](monkeypatch)
    assert backend.access("dostal-x") == OPAQUE_BYTES


@pytest.mark.parametrize("provider", ["gcp", "aws"])
def test_cli_not_on_path_raises_backend_error(monkeypatch, provider):
    monkeypatch.setattr("shutil.which", lambda _: None)
    backend = GcloudBackend() if provider == "gcp" else AwsBackend()
    with pytest.raises(BackendError):
        backend.access("dostal-x")


@pytest.mark.parametrize("provider", ["gcp", "aws"])
def test_cli_failure_raises_backend_error_without_leaking_value(monkeypatch, provider):
    # A real access-denied stderr names the reference, never the plaintext —
    # the CLI never had the value in the first place when the call fails.
    monkeypatch.setattr("shutil.which", lambda _: "/usr/bin/tool")
    monkeypatch.setattr(subprocess, "run", _fake_run_fail("PermissionDenied: dostal-x"))
    backend = GcloudBackend() if provider == "gcp" else AwsBackend()
    with pytest.raises(BackendError) as exc:
        backend.access("dostal-x")
    assert OPAQUE_BYTES not in str(exc.value)


def test_mock_unknown_secret_raises_backend_error():
    with pytest.raises(BackendError):
        MockBackend().access("nope")


# --- Azure: interface-conformant, cleanly unimplemented ---------------------

def test_azure_is_instance_of_protocol():
    assert isinstance(AzureBackend(), SecretBackend)


def test_azure_access_raises_backend_error_not_a_crash():
    with pytest.raises(BackendError):
        AzureBackend().access("dostal-x")


def test_azure_never_shells_out(monkeypatch):
    def _boom(*a, **k):
        raise AssertionError("AzureBackend must not touch the network/subprocess")
    monkeypatch.setattr(subprocess, "run", _boom)
    with pytest.raises(BackendError):
        AzureBackend().access("dostal-x")


# --- factory -----------------------------------------------------------------

@pytest.mark.parametrize("provider,cls", [
    ("gcp", GcloudBackend), ("aws", AwsBackend), ("azure", AzureBackend),
])
def test_get_backend_selects_by_name(provider, cls):
    assert isinstance(get_backend(provider), cls)


def test_get_backend_forwards_kwargs():
    assert get_backend("gcp", project="proj").project == "proj"
    assert get_backend("aws", region="us-east-1").region == "us-east-1"


def test_get_backend_rejects_unknown_provider():
    with pytest.raises(BackendError):
        get_backend("bogus-cloud")
