# Architecture

Portunus is the secret broker system for the Pantheon ecosystem. It ensures that agents and language models never see plaintext secrets in their contexts. Instead, they reference secrets by name (e.g., `{{secret:slack-bot-token}}`), and Portunus resolves these at the execution boundary.

## Component Flow

```mermaid
flowchart TD
    subgraph Execution Boundary
        Agent[Pantheon Agent / LLM Context]
    end

    subgraph Portunus
        Ostiarius[OSTIARIUS<br/>Resolver CLI/API]
        Petitio[PETITIO<br/>Approval Gate]
        Arca[ARCA<br/>Vault Tier]
        Audit[Audit Log]
    end

    subgraph Backends
        Local[LocalEncryptedBackend]
        GCP[GCP Secret Manager]
    end

    Agent -- "{{secret:NAME}}" --> Ostiarius
    Ostiarius -- Validates Request --> Petitio
    Petitio -- Logs Decision --> Audit
    Petitio -- "Approved/Gated" --> Arca
    Arca --> Local
    Arca --> GCP
    Ostiarius -- "Injects Plaintext" --> API[Outbound API / Tool]
    
    style Agent fill:#f9f,stroke:#333,stroke-width:2px
    style API fill:#ccf,stroke:#333,stroke-width:2px
```

## Role in Pantheon
Portunus is the fundamental security component of the Dostal harness. It ensures that API keys, database credentials, and session states are securely managed, audited, and never leaked to an agent's context.

### Toggle & A/B Metrics
*(Standard OSS Note: Portunus currently relies on fixed configuration for its backends. A/B testing of backend performance or gating policies is on the roadmap but not yet implemented.)*
