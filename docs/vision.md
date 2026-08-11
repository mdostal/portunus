# Vision

## Current State
Portunus successfully isolates secrets from the LLM context within the Pantheon ecosystem. It provides the **OSTIARIUS** resolver, the **ARCA** local encrypted and GCP Secret Manager tiers, and the **Petitio** approval gates, backed by a tamper-evident audit log. 

## Goals
- Provide seamless, secure secret injection for all Pantheon plugins and tools without developer overhead.
- Ensure 100% auditability for every secret accessed by an agent.
- Keep the system easy to self-host and verify.

## Long-term Vision
In the future, Portunus aims to expand its capabilities to include:
- **Dynamic Temporary Credentials:** Issuing short-lived, just-in-time tokens instead of static API keys for supported services.
- **Advanced Policy Engine:** More granular, context-aware approval policies (e.g., gating based on the time of day, risk level of the request, or the specific agent invoking the tool).
- **Federated Secrets:** Allowing cross-harness secret sharing with robust zero-trust policies.
