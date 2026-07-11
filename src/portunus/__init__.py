"""Portunus — the Dostal harness secret broker.

Named for the Roman god of keys and gates. Portunus keeps a *reference*
registry (name -> Secret Manager location, never the value) and resolves a
``{{secret:NAME}}`` placeholder to a live value ONLY at the execution
boundary (the outbound API/tool/build call). The plaintext value never enters
an LLM/agent context, a log line, the board, or a return value handed back up
the stack.

Public surface:
    Registry        reference registry (name -> SM path); never stores a value
    AuditChain      tamper-evident hash-chain access log
    Broker          grant / gate / approve + lifecycle guard, wired to audit
    Resolver        boundary-only placeholder resolution
    MockBackend     in-memory backend for tests
    LocalVault      local encrypted-at-rest backend (Keychain master key)
    GcloudBackend   GCP Secret Manager backend (shells to gcloud)
"""
from .registry import Registry, Reference
from .audit import AuditChain
from .backend import SecretBackend, MockBackend, GcloudBackend, BackendError
from .localvault import (
    LocalVault,
    KeychainKeyProvider,
    FileKeyProvider,
    LocalKeyError,
    VaultIntegrityError,
)
from .broker import Broker, NotInjectable, ApprovalRequired
from .resolver import Resolver, UnknownReference, PLACEHOLDER_RE

__version__ = "0.1.0"

__all__ = [
    "Registry",
    "Reference",
    "AuditChain",
    "SecretBackend",
    "MockBackend",
    "GcloudBackend",
    "BackendError",
    "LocalVault",
    "KeychainKeyProvider",
    "FileKeyProvider",
    "LocalKeyError",
    "VaultIntegrityError",
    "Broker",
    "NotInjectable",
    "ApprovalRequired",
    "Resolver",
    "UnknownReference",
    "PLACEHOLDER_RE",
    "__version__",
]
