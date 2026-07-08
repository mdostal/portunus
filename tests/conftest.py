"""Shared fixtures — every test gets an isolated PORTUNUS_HOME so nothing
touches the real state directory or GCP."""
import importlib

import pytest


@pytest.fixture
def home(tmp_path, monkeypatch):
    monkeypatch.setenv("PORTUNUS_HOME", str(tmp_path))
    monkeypatch.delenv("DOSTAL_SECRETS_HOME", raising=False)
    monkeypatch.setenv("USER", "tester")
    monkeypatch.delenv("DOSTAL_AGENT", raising=False)
    monkeypatch.delenv("DOSTAL_TASK", raising=False)
    # paths.home() reads the env each call, so no module reload needed.
    return tmp_path


@pytest.fixture
def stack(home):
    """A wired registry + audit + broker + resolver over a MockBackend."""
    from portunus import Registry, AuditChain, Broker, Resolver, MockBackend

    registry = Registry()
    audit = AuditChain()
    broker = Broker(registry, audit)
    backend = MockBackend()
    resolver = Resolver(registry, backend, broker)
    return {
        "registry": registry, "audit": audit, "broker": broker,
        "backend": backend, "resolver": resolver,
    }
