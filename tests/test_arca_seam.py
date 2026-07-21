"""ARCA seam conformance — the plug-and-play contract every vault tier obeys.

These tests pin the seam future cloud adapters (GCP SM, AWS SM, Vault, ...)
must slot into, and pin the canonical build order: LOCAL-FIRST — the local
encrypted vault is the default tier; cloud is explicit opt-in and read-only
until its slice lands.
"""
import json

import pytest

from portunus.backend import ArcaBackend, BackendError, GcloudBackend, MockBackend
from portunus.localvault import LocalVault

SECRET = "sk-seam-test-super-secret-98765"


@pytest.fixture
def vault(home, monkeypatch):
    monkeypatch.setenv("PORTUNUS_KEY_PROVIDER", "file")
    return LocalVault()


# --- seam conformance -------------------------------------------------------
def test_localvault_satisfies_arca_seam(vault):
    assert isinstance(vault, ArcaBackend)


def test_mock_satisfies_arca_seam():
    assert isinstance(MockBackend(), ArcaBackend)


def test_gcloud_satisfies_arca_seam_shape():
    # The methods exist (the seam is visible) even though writes fail closed.
    assert isinstance(GcloudBackend(), ArcaBackend)


# --- CRUD through the seam, identically on every complete tier --------------
@pytest.fixture(params=["mock", "local"])
def tier(request, vault):
    return MockBackend() if request.param == "mock" else vault


def test_seam_set_get_roundtrip(tier):
    tier.set("seam-secret", SECRET)
    assert tier.access("seam-secret") == SECRET


def test_seam_set_overwrites_to_latest(tier):
    tier.set("seam-secret", "old-value")
    tier.set("seam-secret", SECRET)
    assert tier.access("seam-secret") == SECRET


def test_seam_list_names_never_values(tier):
    tier.set("alpha", SECRET)
    tier.set("beta", "other-value")
    names = tier.list_names()
    assert names == ["alpha", "beta"]
    assert all(SECRET not in n for n in names)


def test_seam_delete(tier):
    tier.set("seam-secret", SECRET)
    assert tier.delete("seam-secret") is True
    assert tier.delete("seam-secret") is False
    with pytest.raises(BackendError):
        tier.access("seam-secret")


def test_seam_missing_secret_raises(tier):
    with pytest.raises(BackendError):
        tier.access("never-stored")


# --- no plaintext at rest via the seam --------------------------------------
def test_seam_set_leaves_no_plaintext_on_disk(vault, home):
    vault.set("seam-secret", SECRET)
    on_disk = [p for p in home.rglob("*") if p.is_file()]
    assert on_disk, "vault wrote nothing"
    for p in on_disk:
        assert SECRET.encode() not in p.read_bytes(), f"plaintext leaked into {p}"


def test_seam_wrong_master_key_fails_closed(vault, home):
    vault.set("seam-secret", SECRET)
    keyfile = home / "local" / "master.key"
    assert keyfile.exists()
    keyfile.write_text("00" * 32 + "\n")
    fresh = LocalVault()
    with pytest.raises(BackendError):
        fresh.access("seam-secret")


# --- local-first is the DEFAULT (canonical build order, do not invert) ------
def test_default_backend_is_local_vault(home, monkeypatch):
    monkeypatch.delenv("PORTUNUS_BACKEND", raising=False)
    monkeypatch.setenv("PORTUNUS_KEY_PROVIDER", "file")
    from portunus.cli import _build
    *_, resolver = _build()
    assert isinstance(resolver.backend, LocalVault)


def test_gcloud_backend_requires_explicit_opt_in(home, monkeypatch):
    monkeypatch.setenv("PORTUNUS_BACKEND", "gcloud")
    from portunus.cli import _build
    *_, resolver = _build()
    assert isinstance(resolver.backend, GcloudBackend)


def test_gcloud_write_seam_fails_closed():
    g = GcloudBackend()
    with pytest.raises(BackendError):
        g.set("x", "y")
    with pytest.raises(BackendError):
        g.list_names()
    with pytest.raises(BackendError):
        g.delete("x")
