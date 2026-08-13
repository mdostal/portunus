"""AWS Secrets Manager backend -- STUB (story 06). No real AWS calls."""
import ast
import inspect
from pathlib import Path

import pytest

from portunus.backend import AWSSecretsManagerBackend, BackendError, SecretBackend


def test_stub_raises_backend_error_naming_itself_not_implemented():
    backend = AWSSecretsManagerBackend()
    with pytest.raises(BackendError, match="not yet implemented"):
        backend.access("any-name")


def test_stub_conforms_to_secret_backend_protocol():
    assert isinstance(AWSSecretsManagerBackend(), SecretBackend)


def test_stub_makes_zero_network_or_aws_sdk_imports():
    src = Path("src/portunus/backend.py").read_text()
    assert "boto3" not in src
    assert "botocore" not in src


def test_stub_access_never_calls_out_before_raising():
    """The raise must be the first thing access() does -- no partial work,
    no accidental network attempt before the NotImplementedError-equivalent."""
    src = inspect.getsource(AWSSecretsManagerBackend.access)
    # Only a docstring + a single raise statement in the body.
    import textwrap
    tree = ast.parse(textwrap.dedent(src))
    func = tree.body[0]
    body = func.body[1:] if ast.get_docstring(func) else func.body
    assert len(body) == 1
    assert isinstance(body[0], ast.Raise)
