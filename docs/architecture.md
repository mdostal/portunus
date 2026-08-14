# Portunus Architecture

This is the adopter-facing reference — the one page meant to answer "how does this actually
work" without reading five source files first. For narrative/prose context see the main
[README](../README.md); for the historical design record behind these decisions see
`.pHive/epics/*/docs/`.

## 1. Component diagram

Three OSTIARIUS entry points, one implementation underneath. Every request passes through
Petitio before ARCA ever gives up a value, and every decision — allowed or denied — lands in
the audit chain.

```mermaid
graph TD
    accTitle: Portunus component diagram
    accDescr: Three OSTIARIUS entry points funnel into one Resolver/Broker implementation, which asks Petitio before ARCA ever returns a value, with the audit chain recording every decision.

    CLI["portunus CLI<br/>(cli.py)"]
    UIRoutes["UI API routes<br/>(ui/app/api/*/route.ts)"]
    MCP["MCP server<br/>(mcp_server.py, portunus mcp)"]

    subgraph OSTIARIUS["OSTIARIUS — the gatekeeper API (one implementation, three entry points)"]
        Resolver["Resolver<br/>(resolver.py)"]
    end

    subgraph Petitio["Petitio — the approval-gate wrapper"]
        Broker["Broker.check_injectable()<br/>(broker.py)<br/>lifecycle guard + approval gate<br/>Identity/requester: wired, not yet enforced"]
    end

    subgraph ARCA["ARCA — the vault store (per-reference/per-project router)"]
        Router["backend router<br/>(_make_backend_router)"]
        Real["Real backends<br/>Local · GCP (+ sync-down cache)"]
        Stub["Honest stubs<br/>AWS · Vault · Infisical · Doppler · 1Password · Azure"]
    end

    Audit[("Audit chain<br/>(audit.py)<br/>tamper-evident hash chain")]

    CLI --> Resolver
    UIRoutes -->|"shells out to the CLI"| CLI
    MCP --> Resolver

    Resolver --> Broker
    Broker -->|allowed| Router
    Router --> Real
    Router --> Stub

    Broker -.->|every decision| Audit
    Real -.->|every resolve| Audit
```

## 2. ARCA backend-selection precedence

A reference resolves through whichever backend actually applies to it — never one global
`PORTUNUS_BACKEND` choice for the whole process. Three levels, checked in order:

```mermaid
graph TD
    accTitle: ARCA backend-selection precedence
    accDescr: A reference's backend is resolved by checking its own override first, then its project's VaultBinding, then falling back to the global PORTUNUS_BACKEND-selected backend.

    Start(["Resolving a Reference"]) --> RefBackend{"ref.backend set?"}
    RefBackend -->|yes| UseRefBackend["Use that backend directly<br/>(local / gcp / aws / vault / infisical / doppler / onepassword / azure)"]
    RefBackend -->|no, empty| ProjectBinding{"VaultBinding for<br/>ref.project exists?"}
    ProjectBinding -->|yes| CheckSyncMode{"binding.backend == gcp<br/>AND sync_mode == cached?"}
    CheckSyncMode -->|yes| SyncingBackend["SyncingBackend<br/>(GcloudBackend + LocalEncryptedBackend,<br/>offline-resilient)"]
    CheckSyncMode -->|no| UseBindingBackend["Use binding.backend directly"]
    ProjectBinding -->|no| GlobalFallback["Global fallback:<br/>today's PORTUNUS_BACKEND env var<br/>(local / gcloud / aws / mock)"]

    UseRefBackend --> Resolve(["backend.access(sm_name, project)"])
    UseBindingBackend --> Resolve
    SyncingBackend --> Resolve
    GlobalFallback --> Resolve
```

`PORTUNUS_BACKEND=mock` always short-circuits this entire tree — every reference resolves
through the single `MockBackend` regardless of any configured binding, a safety rail for tests
and dry runs.

## 3. Petitio: today vs. the designed future

`Identity` and `check_injectable`'s `requester` parameter exist today as a deliberately inert
seam — every caller is currently allowed regardless of who's asking. The state machine below
is the *designed target*, not yet built; no `PolicyStore`/`EscalationRequest` code exists yet.

```mermaid
graph TD
    accTitle: Petitio today versus the designed future
    accDescr: Today, check_injectable only checks lifecycle state and approval, ignoring the requester entirely. The designed future adds a policy check and an escalation request-review-grant flow before falling back to deny.

    subgraph Today["Today — real, shipped"]
        T1["check_injectable(name, requester=None)"] --> T2{"lifecycle state<br/>enabled/locked?"}
        T2 -->|no| T3["NotInjectable<br/>(fail closed)"]
        T2 -->|yes| T4{"approval gate<br/>required?"}
        T4 -->|yes, no valid approval| T5["ApprovalRequired"]
        T4 -->|no, or valid approval| T6["Allowed<br/>(requester is never consulted)"]
    end

    subgraph Future["Designed, not yet built"]
        F1["requester has a<br/>PolicyStore grant?"] -->|yes| T6
        F1 -->|no| F2["EscalationRequest: pending"]
        F2 --> F3{"approver decision"}
        F3 -->|approved, within TTL| T6
        F3 -->|denied| F4["Denied (terminal)"]
        F3 -->|TTL lapses| F5["Expired"]
    end

    T6 -.->|future wiring point| F1
```

## 4. Request/resolve sequence

The invariant this diagram exists to make legible: **the approver (today: nobody, since
there's no enforcement yet) never touches the plaintext.** Only the resolver's own boundary
sink (env var, file, or exec argv) ever holds the value, and it's never returned up the stack.

```mermaid
sequenceDiagram
    accTitle: Portunus request/resolve sequence
    accDescr: A caller resolves a placeholder through OSTIARIUS, Petitio checks injectability, ARCA fetches the value, and the resolver injects it at the boundary sink without ever returning it.

    participant Caller as Caller (CLI/UI/MCP)
    participant Resolver as OSTIARIUS (Resolver)
    participant Broker as Petitio (Broker)
    participant ARCA as ARCA (backend)
    participant Audit as Audit chain
    participant Sink as Boundary sink<br/>(env var / file / exec argv)

    Caller->>Resolver: resolve {{secret:NAME}}
    Resolver->>Broker: check_injectable(name)
    Broker->>Broker: lifecycle + approval check<br/>(requester param: inert today)
    alt not injectable
        Broker-->>Resolver: raise NotInjectable / ApprovalRequired
        Broker->>Audit: append(denied-*)
        Resolver-->>Caller: error (never a value)
    else injectable
        Broker-->>Resolver: Reference (metadata only)
        Resolver->>ARCA: backend.access(sm_name, project)
        ARCA-->>Resolver: plaintext value
        Resolver->>Sink: inject value directly
        Resolver->>Audit: append(resolve, ok)
        Resolver-->>Caller: success signal only<br/>(never the value)
    end
```

## 5. Rotation provenance

A second, genuinely different kind of "real vs. stub" split from ARCA's own (§2, §1's `Stub`
node): ARCA answers *where a value lives*, `RotationBinding`/`RotationAdapter` (`rotation.py`)
answer *who would rotate it, and whether Portunus can yet*. **Zero real adapters exist today —
every provider is `status="stub"`.** This section documents the shape now so the docs don't
overstate what's built; treat every "would" below as aspirational, not shipped.

```mermaid
graph TD
    accTitle: Rotation provenance -- config today, real integration later
    accDescr: RotationBinding records which provider and what account context; RotationAdapter is a stub registry today. A future real adapter fetches its own admin credential via Portunus's own resolver, never a special-cased credential path.

    RB["RotationBinding<br/>(provider, status: real|stub, account)<br/>PORTUNUS_HOME/rotation-bindings.json"]
    Registry["RotationAdapter registry<br/>Vercel (priority target) · GitHub · Stripe"]
    Button["DetailDrawer 'Auto-rotate…' button<br/>enabled only if status==real"]

    RB -->|drives| Button
    Registry -->|every adapter today| Stub["raises RotationAdapterError<br/>(no real API call, ever)"]

    subgraph Future["Once a real adapter ships (none do yet)"]
        Real["VercelRotationAdapter.rotate(ref)"] -->|resolves its OWN admin token via| SelfResolve["resolver.resolve_call(<br/>'{{secret:portunus-admin-vercel-token}}', boundary=...)"]
        SelfResolve -->|same boundary-only sink<br/>every other value uses| ProviderAPI["Provider's rotate API"]
    end

    Registry -.->|becomes, once real| Real
```

The recursive property worth naming explicitly: a real rotation adapter authenticates to its
provider using a credential that is **itself just another Portunus-managed `Reference`** —
resolved through the same `Resolver.resolve_call()` boundary sink every other value in this
codebase already uses, never hardcoded into the adapter and never handled outside the normal
resolve path. Portunus would rotate *other* secrets by using *its own* vaulted admin secret — no
second credential-handling mechanism, ever. This is a design decision recorded ahead of the
build, not a description of running code.

## See also

- [README.md](../README.md) — component model table, install/usage, MCP tool reference
- `.pHive/CONTEXT.md` — terminology glossary
- `.pHive/epics/portunus-swappable-trio/docs/` — the research and design record behind this
  page (6-product vault-adapter research, RBAC/escalation-pattern research, OSS
  adapter-marketplace UX research)
