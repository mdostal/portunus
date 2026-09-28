"""Shared fixtures — every test gets an isolated PORTUNUS_HOME so nothing
touches the real state directory or GCP."""
import shutil

import pytest


@pytest.fixture(autouse=True)
def _isolated_portunus_home(tmp_path_factory, monkeypatch):
    """Autouse: no test can ever read or write the real ~/.portunus, even
    one that forgets to request `home`. Tests that need the path request
    `home`, which overrides this with their own tmp_path."""
    monkeypatch.setenv("PORTUNUS_HOME", str(tmp_path_factory.mktemp("portunus-home")))
    monkeypatch.delenv("DOSTAL_SECRETS_HOME", raising=False)


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


@pytest.fixture
def gcloud_on_path(monkeypatch):
    """Make the `shutil.which("gcloud")` guards (discover, backend, cli) see a
    gcloud binary, for tests that stub the gcloud subprocess itself. Other
    lookups fall through to the real `which`."""
    real_which = shutil.which

    def which(cmd, *args, **kwargs):
        if cmd == "gcloud":
            return "/bin/gcloud"
        return real_which(cmd, *args, **kwargs)

    monkeypatch.setattr(shutil, "which", which)
    return "/bin/gcloud"

