# Horizontal Planning Scan: portunus-standalone-core

## 1. Layer Inventory

- **Registry (metadata)** — `registry.py`. Holds `Reference` records (name, sm_name, scope,
  kind, state, approval, sm_path). No structured tags today. Every other layer reads/writes
  through this.
- **Resolver / Injection (OSTIARIUS)** — `resolver.py`. Boundary-only `{{secret:NAME}}`
  substitution today; needs new adapter sinks.
- **Broker (Petitio)** — `broker.py`. Approval gate + lifecycle guard. Every new write/read
  path (UI, agent surface) must route through this, not around it.
- **Backend (ARCA)** — `backend.py`, `localvault.py`. `MockBackend`, `LocalEncryptedBackend`,
  `GcloudBackend`. Mostly unaffected by this epic except a possible write-lock addition.
- **Audit** — `audit.py`. Hash-chain log. Every new entry-producing path (adapters, semantic
  ops, UI writes) needs a new entry type here.
- **CLI** — `cli.py`. Existing subcommands (`drop`, `state`, `resolve`, ...); needs a new
  semantic subcommand for Slice 3.
- **Agent-facing surface (new)** — doesn't exist yet. The thin layer between "agent expresses
  intent in natural language" and "CLI/API call with a concrete tag set."
- **UI (new)** — doesn't exist yet. Small Next.js/React app, localhost-only for v1.
- **Infra/config** — `PORTUNUS_HOME`, `pyproject.toml`, CI (`ci.yml`). Needs new deps for the
  UI (Node toolchain) and possibly a lock library for the registry.

## 2. Per-Layer Requirements

```
## Layer: Registry (metadata)

SCHEMA CHANGES:
  - Reference gains: provider (str), project (str), env (str), tags (dict, open)
  - Keep scope/kind for back-compat; existing records migrate (scope/kind -> tags on read,
    or a one-time migration script — decided in vertical plan)
  - New: resolve_by_tags(partial_tags) -> Reference | raises AmbiguousMatch | raises NoMatch

CONCURRENCY:
  - File-level write lock around registry mutation (flock or equivalent) — new UI/agent
    writers make this a real requirement, not optional

---

## Layer: Resolver / Injection (OSTIARIUS)

NEW SINKS (adapters):
  - EnvVarAdapter — inject into process env (distinct from existing subprocess-argv sink)
  - FileAdapter — templated file write (.env, JSON, YAML), building on the existing 0600
    temp-file sink but parameterized by target format
  - HttpHeaderAdapter — inject into an outbound HTTP request's header (fast-follow, not v1)
  - HttpBodyAdapter — inject into an outbound HTTP request's JSON body field (fast-follow)

SEMANTIC PARSING:
  - parse_intent(natural_language) -> tag_set | AmbiguousIntent (fails closed, no guessing)

---

## Layer: Broker (Petitio)

GATE CHANGES:
  - check_injectable already exists; new adapters and the semantic-op path must call it,
    not bypass it
  - Add/rotate operations (UI "add" form, agent "add" request) route through the same
    harness-side-only local drop path — no new privileged bypass

---

## Layer: Backend (ARCA)

CHANGES:
  - None required for tag resolution (tags live in Registry, not backend)
  - Possible: write-lock coordination if LocalEncryptedBackend's file write needs to
    coordinate with Registry's file write (same PORTUNUS_HOME directory)

---

## Layer: Audit

NEW ENTRY TYPES:
  - adapter_resolution (ref, adapter_kind, target_desc, timestamp — never value)
  - semantic_op (ref-or-null, request_kind [fetch|add|rotate], timestamp)
  - ui_action (ref, action [view|add|rotate|move], timestamp, actor=ui)

VERIFICATION:
  - portunus verify must pass against a chain containing every new entry type

---

## Layer: CLI

NEW SUBCOMMANDS:
  - portunus ask "<natural language request>" — Slice 3 semantic front door
  - portunus inject --tags provider=vercel,project=mdostal.com --target env — adapter dispatch

---

## Layer: Agent-facing surface (new)

SURFACE:
  - CLI subcommand (portunus ask) is the v1 surface
  - Thin Claude skill wrapping the CLI subcommand (so an agent invokes a tool call, not a
    raw shell command it has to construct itself)
  - MCP server: explicitly deferred unless a concrete need arises

---

## Layer: UI (new)

SCREENS:
  - Reference list (view-only: name, tags, state — never value)
  - Reference detail (audit trail for that reference)
  - Add secret form (submits to local drop path, localhost-only, no logging of the value)
  - Rotate action (triggers a server-side rotation flow, not a human-entered new value where
    avoidable)

STACK:
  - Next.js/React, localhost-only dev/run for v1 (no public deployment yet — deployment is
    out of scope until the standalone-vs-plugin question is revisited)

---

## Layer: Infra/config

CHANGES:
  - pyproject.toml: no new Python deps for Slices 1-3 (stdlib + existing cryptography dep
    sufficient); Slice 4 adds a separate Node/Next.js toolchain, not a Python dependency
  - CI: ci.yml already auto-detects python; a Node stack in a UI subdirectory needs a
    decision on whether it's covered by the same CI gate or a separate workflow (open item
    for the UI slice's own design pass)
```

## 3. Cross-Layer Dependencies

```
DEPENDENCIES:

Resolver adapters (Slice 2)        -> Registry resolve_by_tags() (Slice 1) — adapters need a
                                       resolved Reference before they can inject anything
Agent surface parse_intent (Slice 3)-> Registry resolve_by_tags() (Slice 1) — parsed tags feed
                                       the same resolution function, same fail-closed contract
CLI "portunus ask" (Slice 3)       -> Resolver adapters (Slice 2) — the semantic front door
                                       has to dispatch to a real adapter once resolved
UI reference list/detail (Slice 4) -> Registry schema + Audit entries (Slice 1) — UI reads the
                                       new tag fields and the new audit entry types
UI add/rotate (Slice 4)            -> Broker gated local-drop path (existing) — UI writes must
                                       not create a second privileged path
Audit new entry types (all slices) -> Audit layer (existing hash-chain) — every new
                                       write/resolve path needs a corresponding entry type
Registry write lock (Slice 1)      -> Backend file writes (LocalEncryptedBackend) — same
                                       PORTUNUS_HOME directory, lock scope needs to cover both
```

## 4. Layer Map Diagram

```
HORIZONTAL LAYER MAP
─────────────────────────────────────────────────────────────────────────

Registry     │ tags/provider/  │ resolve_by_    │ write lock     │            │
             │ project/env     │ tags()         │ (concurrency)  │            │
─────────────┼─────────────────┼────────────────┼────────────────┼────────────┤
Resolver     │                 │ EnvVarAdapter  │ HttpHeader/    │ parse_     │
             │                 │ FileAdapter    │ Body (later)   │ intent()   │
─────────────┼─────────────────┼────────────────┼────────────────┼────────────┤
Broker       │ (existing gate  │ used by every  │ used by UI     │ used by    │
             │  reused as-is)  │ new adapter    │ add/rotate     │ semantic op│
─────────────┼─────────────────┼────────────────┼────────────────┼────────────┤
Audit        │                 │ adapter_       │ ui_action      │ semantic_  │
             │                 │ resolution     │ entries        │ op entries │
─────────────┼─────────────────┼────────────────┼────────────────┼────────────┤
CLI          │                 │ portunus       │                │ portunus   │
             │                 │ inject         │                │ ask        │
─────────────┼─────────────────┼────────────────┼────────────────┼────────────┤
Agent surface│                 │                │                │ Claude     │
             │                 │                │                │ skill      │
─────────────┼─────────────────┼────────────────┼────────────────┼────────────┤
UI           │ reference list  │ reference      │ add secret     │ rotate     │
             │ (view tags)     │ detail (audit) │ form           │ action     │
─────────────────────────────────────────────────────────────────────────
```

## 5. Scope Summary

```
HORIZONTAL SCOPE:
  Layers affected: 8 (Registry, Resolver, Broker, Backend, Audit, CLI, Agent surface, UI)
  Total items: ~24 (schema/adapters/CLI subcommands/audit entry types/UI screens combined)
  New vs modified: ~18 new, ~6 modified
  Estimated total effort: large

  LARGEST LAYER: UI (net-new app, 4 screens/flows, new stack)
  RISKIEST LAYER: Registry (concurrency + migration + the fail-closed resolution contract
    everything else depends on) — get this wrong and every downstream layer inherits the bug
```
