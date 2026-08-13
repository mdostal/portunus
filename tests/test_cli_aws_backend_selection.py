"""PORTUNUS_BACKEND=aws selects the AWS stub, not a silent LocalEncryptedBackend
fallback (story 06, grill V1 -- the real pre-epic gap: an unrecognized
backend_kind silently fell through to `else` -> LocalEncryptedBackend)."""
from portunus.backend import AWSSecretsManagerBackend
from portunus.cli import _build


def test_portunus_backend_aws_selects_the_stub_not_local_fallback(home, monkeypatch):
    monkeypatch.setenv("PORTUNUS_BACKEND", "aws")
    _registry, _audit, _broker, resolver = _build()
    assert isinstance(resolver.backend, AWSSecretsManagerBackend)
