import json
import os
import tempfile
from contextlib import contextmanager
from typing import Generator
from .cli import _build

@contextmanager
def inject_session(site: str, account: str, agent_role: str = "default") -> Generator[str, None, None]:
    """
    Context manager that loads a Portunus session for the given site and account,
    writes it to a secure temporary file, yields the file path for Playwright
    storageState injection, and securely removes it afterwards.
    """
    registry, _, broker, resolver = _build()
    backend = resolver.backend
    
    if not hasattr(backend, "load_session"):
        raise RuntimeError("playwright injection requires LocalEncryptedBackend")

    record = backend.load_session(site, account)
    broker.check_session_access(site, account, record, agent_role)
    session_data = record["session"]
    
    fd, path = tempfile.mkstemp(prefix="portunus-pw-", suffix=".json")
    try:
        os.fchmod(fd, 0o600)
        with os.fdopen(fd, "w") as f:
            json.dump(session_data, f)
        yield path
    finally:
        try:
            os.unlink(path)
        except OSError:
            pass
