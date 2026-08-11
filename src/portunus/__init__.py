"""Portunus — the Dostal harness secret broker.

Named for the Roman god of keys and gates. Portunus keeps a *reference*
registry (name -> Secret Manager location, never the value) and resolves a
``{{secret:NAME}}`` placeholder to a live value ONLY at the execution
boundary (the outbound API/tool/build call). The plaintext value never enters
an LLM/agent context, a log line, the board, or a return value handed back up
the stack.

Portunus is the WHOLE system; its components carry their own names:
    OSTIARIUS   the gatekeeper API — the request/deposit boundary (Resolver + CLI)
    ARCA        the vault store — local-encrypted + GCP SM tiers (backends)
    Petitio     the approval-gate wrapper — every request is gated (Broker)

Public surface:
    Registry        reference registry (name -> SM path); never stores a value
    AuditChain      tamper-evident hash-chain access log
    Broker          Petitio — grant / gate / approve + lifecycle guard, wired to audit
    Resolver        OSTIARIUS — boundary-only placeholder resolution
    MockBackend     ARCA, in-memory backend for tests
    LocalEncryptedBackend  ARCA, Stage 1 local-encrypted tier (default backend)
    GcloudBackend   ARCA, GCP Secret Manager backend (shells to gcloud; Stage 2+)
"""
from .registry import Registry, Reference
from .audit import AuditChain
from .backend import SecretBackend, MockBackend, GcloudBackend, BackendError
from .localvault import LocalEncryptedBackend, SESSION_SCHEMA
from .broker import Broker, NotInjectable, ApprovalRequired
from .resolver import Resolver, UnknownReference, PLACEHOLDER_RE

__version__ = "0.1.0"

__all__ = [
    "Registry",
    "Reference",
    "AuditChain",
    "SecretBackend",
    "MockBackend",
    "LocalEncryptedBackend",
    "SESSION_SCHEMA",
    "GcloudBackend",
    "BackendError",
    "Broker",
    "NotInjectable",
    "ApprovalRequired",
    "Resolver",
    "UnknownReference",
    "PLACEHOLDER_RE",
    "__version__",
]
