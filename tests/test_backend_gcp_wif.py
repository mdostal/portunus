"""Multi-project GCP backend: per-project WIF credential binding (story 03)."""
import json
import os
import stat
from pathlib import Path
from types import SimpleNamespace

from portunus import AuditChain, GCPWorkloadIdentityAuth, OIDCToken
from portunus.backend import GcloudBackend, GcpProjectBinding, load_gcp_bindings, save_gcp_bindings


class StaticOIDC:
    def __init__(self, token="OIDC.JWT.TEST"):
        self.token = OIDCToken(
            token=token, issuer="https://issuer.example",
            subject="agent:dostal-dev", audience="portunus", expires_at=0,
        )

    def get(self):
        return self.token


def _mocked_transport(access_token):
    return lambda url, data, headers, timeout: {"access_token": access_token, "expires_in": 900}


def test_load_gcp_bindings_falls_back_to_env_when_no_file(home, monkeypatch):
    monkeypatch.setenv("PORTUNUS_GCP_PROJECT", "personalsites-487021")
    monkeypatch.setenv("PORTUNUS_GCP_WIF_AUDIENCE", "//iam.googleapis.com/projects/1/.../providers/p")
    bindings = load_gcp_bindings()
    assert bindings["personalsites-487021"].project == "personalsites-487021"
    assert bindings["personalsites-487021"].wif_audience.startswith("//iam.googleapis.com")


def test_load_gcp_bindings_reads_bindings_file(home):
    save_gcp_bindings({
        "personalsites-487021": GcpProjectBinding("personalsites-487021", "aud-a"),
        "firefly-events-inc": GcpProjectBinding("firefly-events-inc", "aud-b"),
    })
    bindings = load_gcp_bindings()
    assert bindings["personalsites-487021"].wif_audience == "aud-a"
    assert bindings["firefly-events-inc"].wif_audience == "aud-b"


def test_gcp_bindings_file_is_0600(home):
    save_gcp_bindings({"p": GcpProjectBinding("p", "aud")})
    path = home / "gcp-bindings.json"
    mode = stat.S_IMODE(os.stat(path).st_mode)
    assert mode == 0o600


def test_backend_uses_project_scoped_binding_over_default(home, monkeypatch):
    monkeypatch.setattr("portunus.backend.shutil.which", lambda name: "/bin/gcloud")
    observed = []

    def runner(cmd, capture_output, text, timeout):
        observed.append(cmd)
        return SimpleNamespace(returncode=0, stdout="ok", stderr="")

    bindings = {
        "personalsites-487021": GcpProjectBinding(
            "personalsites-487021",
            "//iam.googleapis.com/projects/1/locations/global/workloadIdentityPools/p/providers/p",
        ),
    }
    backend = GcloudBackend(
        bindings=bindings, runner=runner, audit=AuditChain(),
    )
    # Patch the internal provider's transport so mint() never hits the network.
    provider = backend._binding_providers["personalsites-487021"]
    provider.transport = _mocked_transport("GCP.TOKEN.A")
    provider.token_source = StaticOIDC()

    backend.access("dostal-x", project="personalsites-487021")
    cmd = observed[0]
    assert "--project=personalsites-487021" in cmd
    assert any(arg.startswith("--access-token-file=") for arg in cmd)


def test_two_different_projects_in_same_process_use_own_bindings(home, monkeypatch):
    monkeypatch.setattr("portunus.backend.shutil.which", lambda name: "/bin/gcloud")
    observed = []

    def runner(cmd, capture_output, text, timeout):
        observed.append(cmd)
        return SimpleNamespace(returncode=0, stdout="ok", stderr="")

    bindings = {
        "personalsites-487021": GcpProjectBinding("personalsites-487021", "aud-personal"),
        "firefly-events-inc": GcpProjectBinding("firefly-events-inc", "aud-firefly"),
    }
    backend = GcloudBackend(bindings=bindings, runner=runner, audit=AuditChain())
    for proj, token in (("personalsites-487021", "TOKEN.A"), ("firefly-events-inc", "TOKEN.B")):
        provider = backend._binding_providers[proj]
        provider.transport = _mocked_transport(token)
        provider.token_source = StaticOIDC()

    backend.access("secret-a", project="personalsites-487021")
    backend.access("secret-b", project="firefly-events-inc")

    assert "--project=personalsites-487021" in observed[0]
    assert "--project=firefly-events-inc" in observed[1]


def test_access_token_file_is_0600_and_deleted_even_on_failure(home, monkeypatch):
    monkeypatch.setattr("portunus.backend.shutil.which", lambda name: "/bin/gcloud")
    observed = {}

    def runner(cmd, capture_output, text, timeout):
        token_arg = next(arg for arg in cmd if arg.startswith("--access-token-file="))
        token_path = Path(token_arg.split("=", 1)[1])
        observed["path"] = token_path
        observed["mode"] = stat.S_IMODE(os.stat(token_path).st_mode)
        observed["text"] = token_path.read_text()
        return SimpleNamespace(returncode=1, stdout="", stderr="denied")

    auth = GCPWorkloadIdentityAuth(
        audience="aud", token_source=StaticOIDC(), audit=AuditChain(),
        transport=_mocked_transport("GCP.TOKEN.C"),
    )
    backend = GcloudBackend(project="p", credential_provider=auth, runner=runner)

    from portunus.backend import BackendError
    import pytest
    with pytest.raises(BackendError):
        backend.access("dostal-x")

    assert observed["mode"] == 0o600
    assert observed["text"] == "GCP.TOKEN.C"
    assert not observed["path"].exists()


def test_build_wires_bindings_into_gcloud_backend(home, monkeypatch):
    """cli._build() end-to-end: PORTUNUS_BACKEND=gcloud picks up gcp-bindings.json."""
    from portunus.cli import _build

    save_gcp_bindings({"personalsites-487021": GcpProjectBinding("personalsites-487021", "aud-x")})
    monkeypatch.setenv("PORTUNUS_BACKEND", "gcloud")
    _registry, _audit, _broker, resolver = _build()
    assert isinstance(resolver.backend, GcloudBackend)
    assert "personalsites-487021" in resolver.backend._binding_providers


def test_no_binding_and_no_credential_provider_falls_back_to_ambient_gcloud(home, monkeypatch):
    monkeypatch.setattr("portunus.backend.shutil.which", lambda name: "/bin/gcloud")
    observed = []

    def runner(cmd, capture_output, text, timeout):
        observed.append(cmd)
        return SimpleNamespace(returncode=0, stdout="ok", stderr="")

    backend = GcloudBackend(project="legacy-project", runner=runner)
    backend.access("dostal-x")
    cmd = observed[0]
    assert "--project=legacy-project" in cmd
    assert not any(arg.startswith("--access-token-file=") for arg in cmd)
