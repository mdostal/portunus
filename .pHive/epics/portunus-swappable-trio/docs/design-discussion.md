# Design Discussion — portunus-swappable-trio

## 1. Goal

Make Portunus's three named components genuinely swappable in framing and honest in what's
real, per the operator's stated vision (OSTIARIUS = porter/API surface, ARCA = vault, Petitio
= access levels — the DOS-508 naming this session traced back to its origin). Scope for
*this* pass, confirmed directly by the operator after a deep-research workflow (10 agents:
6 vault-product researchers, RBAC/escalation-pattern research, OSS adapter-marketplace UX
research, a design synthesis, and an adversarial grill pass):

- **ARCA**: local + GCP stay the only *real* backends — matches what all 6 researched
  candidates (HashiCorp Vault, Infisical, AWS Secrets Manager, Doppler, 1Password Secrets
  Automation, Azure Key Vault) independently recommended (stub-only; none has a validated
  environment to build a real adapter against yet, unlike GCP's real `demo-project-483920`/
  `demo-cicd` projects). Add honest, interface-conformant stubs for the other five, matching
  `AWSSecretsManagerBackend`'s existing restraint exactly. Add one real, scoped capability:
  harden the sync-down cache (`SyncingBackend`, shipped in portunus-vault-routing) to survive
  a real network outage by falling back to the last-known-good local copy instead of hard-
  failing — this is what "disconnected LLMs with vaults" actually requires, and it's a gap in
  what already exists, not a new adapter.
- **Petitio**: pure pass-through stub. `Identity` + an optional `requester` parameter threaded
  through `Broker.check_injectable` as a genuine no-op — no policy engine, no enforcement, no
  rush, per explicit operator instruction ("it just passes through, no rush"). This is Slice 0
  only from the earlier draft; Slices 1+ (PolicyStore, escalation state machine, real
  enforcement) are explicitly deferred to a future epic, not started here.
- **OSTIARIUS**: docs-only clarification — confirmed as the umbrella for the CLI + UI API
  routes + MCP server, already true structurally.
- **Docs + diagrams**: README, `.pHive/CONTEXT.md`, new `docs/architecture.md` with real
  diagrams (component, backend-selection precedence, Petitio's inert-today shape, and the
  request/inject sequence).

## 2. Corrections from the research workflow's own adversarial grill

The auto-synthesized draft this workflow produced had real errors, corrected here before any
code gets written:

- **`SecretBackend` is a one-method Protocol** (`access()` only). `store()`/`latest_version()`
  are duck-typed extras some concrete classes happen to implement, accessed via
  `hasattr()`/`getattr()` (`cli.py`'s drop-backend gate, the router's cached-mode check) — not
  part of the Protocol. New stub backends implement `access()` only (raising `BackendError`),
  matching `AWSSecretsManagerBackend` exactly; they do not need `store()`/`latest_version()`
  stubs at all since nothing calls those on an unrecognized/stub backend today.
- **Remote `store()` (write-to-cloud) is explicitly OUT of scope.** The original draft quietly
  proposed giving Vault/AWS a `store()` as if it were parity with an existing pattern — it
  isn't. `portunus drop`/`portunus_drop` are local-vault-only by explicit design today (the
  `hasattr(backend, "store")` gate exists specifically to keep it that way). This pass doesn't
  touch that boundary, and no new stub backend implements `store()`.
- **No new real cloud/vault PoC this pass** — deferred until a real validated environment
  exists for one (a disposable local Vault dev-mode container has no new-account cost and is
  the cheapest future candidate if the operator wants to greenlight it later; AWS needs a new
  account, a real ask, not assumed here).
- **BUSL/licensing notes are informational, not legal conclusions** — stub docstrings note
  licensing shape (e.g. Vault's BUSL-1.1 applies to whoever runs the server, not client code;
  Infisical's core is genuinely MIT) as a pointer for whoever builds the real adapter later,
  explicitly not asserted as settled legal fact.
- **Vocabulary stays scoped correctly**: new stub `backend` kinds (`vault`, `infisical`,
  `doppler`, `onepassword`, `azure`) are reachable only via `VaultBinding.backend`/
  `ref.backend` (the router) — `_build()`'s legacy `PORTUNUS_BACKEND` global-fallback env var
  keeps its existing four values (`local`/`gcloud`/`aws`/`mock`) unchanged; nothing about this
  pass requires reconciling the router's `"gcp"` vs. the env var's `"gcloud"` naming (a
  pre-existing, separate inconsistency, out of scope here).
- **Teleport is not a viable embed-dependency for future Petitio expansion**: OSS core is
  AGPLv3 (not the Apache-2.0 the operator asked about), and compiled Community Edition
  binaries moved to a commercial license at v16 that explicitly forbids embedding in another
  product — directly conflicting with "bundled in some of my other tools." Noted in docs as
  the reason Petitio's future expansion path is "build our own, interop with Teleport/others
  later," not "embed Teleport."

## 3. ARCA: five honest stubs + one real hardening

### 3.1 New stub backends

`VaultServerBackend` (Vault/OpenBao), `InfisicalBackend`, `DopplerBackend`,
`OnePasswordConnectBackend`, `AzureKeyVaultBackend` — each a small class:

```python
class VaultServerBackend:
    """HashiCorp Vault / OpenBao -- STUB. No real calls.

    Interface-conformant placeholder, matching AWSSecretsManagerBackend's own
    restraint. Vault OSS is BUSL-1.1 (applies to whoever runs the server, not
    this client code); OpenBao (MPL-2.0, Linux Foundation fork, wire-
    compatible) is a self-hosted alternative for a BUSL-averse deployer, once
    a real adapter is built. Request a real adapter: <github-issue-url>.
    """
    def access(self, sm_name: str, project: str = "") -> str:
        raise BackendError(
            "HashiCorp Vault backend is not yet implemented -- "
            "request it: <github-issue-url>"
        )
```

Same shape for the other four, each docstring noting its own real licensing/self-host
character (Infisical: MIT core, self-hostable; Doppler: proprietary SaaS, no viable self-host;
1Password Connect: free container images gated behind a paid Business/Enterprise tenant; Azure
Key Vault: proprietary cloud-only) — accurate, sourced from this session's research, not
marketing copy.

`VaultBinding.backend` recognizes the five new string values; `_make_backend_router`'s
`_for_kind()` gains a branch per stub, all resolving to the matching stub instance (cached
like every other kind). Selecting one of these via `portunus bindings set --backend vault`
(etc.) works today already — the value is stored — this story is about what the router
constructs when it sees that value, and about the resolve path failing closed with a clear
message instead of a `KeyError`/`AttributeError` if it's not yet recognized.

### 3.2 Offline-resilient sync-down (the real capability)

`SyncingBackend.access()` today calls `remote.latest_version()` unconditionally when the
remote supports it — if that call raises `BackendError` (network unreachable, DNS failure,
`gcloud` timeout), the whole `access()` call fails, even when a perfectly good cached copy
already exists locally. This is the actual gap behind "enables it to still work" /
"disconnected LLMs with vaults":

```python
def access(self, sm_name, project=""):
    cache_key = f"{project}:{sm_name}"
    latest_version = getattr(self.remote, "latest_version", None)
    if latest_version is not None:
        state = self._load_state()
        try:
            marker = latest_version(sm_name, project=project)
        except BackendError:
            # Remote unreachable -- serve the last-known-good local copy
            # rather than hard-failing, if one exists. This is what makes
            # a cached-mode project keep working while disconnected.
            try:
                value = self.local.access(cache_key)
                self.last_sync_result = "stale-offline"
                return value
            except BackendError:
                raise  # never synced before AND remote unreachable -- genuinely nothing to serve
        ...
```

`last_sync_result` gains a third value, `"stale-offline"`, distinct from `"synced"`/`"fresh"`
— `portunus_sync`'s report and any future UI surface can show "served from cache, could not
verify freshness" distinctly from "confirmed fresh." This never touches a value it doesn't
already have cached; it changes only the failure-handling path around the recency *check*,
not the boundary invariant.

## 4. Petitio: Identity + inert requester param

- **`Identity`** (new, small dataclass — `broker.py` or a new `identity.py`): `{name: str,
  kind: Literal["human", "agent", "system"]}`. Resolved the same way `AuditChain`'s actor
  already is (`DOSTAL_AGENT` env for agents, `USER` for humans) — no new resolution mechanism,
  reuses the existing seed.
- **`Broker.check_injectable(name, requester: Optional[Identity] = None)`** — new optional
  parameter, defaulting to `None`. The method body does not reference `requester` at all —
  documented explicitly, in the docstring and in a code comment at the exact point a future
  policy check would go, as a deliberate no-op: *"Petitio's access-level enforcement is not
  built yet -- this parameter exists so every call site already threads an identity through,
  ahead of the policy engine that will consume it. Every caller is currently allowed
  regardless of `requester`."* This is load-bearing: a future reviewer must not mistake
  "not yet enforced" for "broken."
- Threaded through (as an optional, defaulted param -- zero behavior change) at every call
  site: `resolver.py::_fetch`, `mcp_server.py`'s resolve/drop/sync tools, `cli.py`'s
  `cmd_resolve`/`cmd_inject`/`cmd_ask`/`cmd_drop`. None of these callers currently pass a real
  `requester` — resolving `Identity.from_env()` and passing it through is deferred to the
  future enforcement epic; this pass only proves the seam compiles and stays behavior-neutral.
- No `PolicyStore`, no `EscalationRequest`, no new CLI/MCP commands. Explicitly out of scope,
  named in docs as "Petitio slice 0 of N" so the boundary is legible to a future reader.

## 5. OSTIARIUS: docs only

No code. README's component table and `.pHive/CONTEXT.md` already claim "three entry points,
one implementation" correctly. This pass adds the architecture diagram (§6) that makes the
claim legible without reading five source files, and names all entry points precisely (ten
`ui/app/api/*/route.ts` routes today, confirmed by directory listing, not miscounted).

## 6. Docs + diagrams

- **README.md**: ARCA row lists local/GCP (real) + the five named stubs with a "request on
  GitHub" pointer; Petitio row gains one sentence noting `Identity`/`requester` exist as an
  inert seam, real enforcement not yet built.
- **`.pHive/CONTEXT.md`**: new terminology entries for `Identity`, the ARCA stub backends, and
  `SyncingBackend`'s offline-fallback behavior.
- **New `docs/architecture.md`** (this repo has no `docs/` dir yet — every design record lives
  under `.pHive/epics/*/docs/`, which is planning history, not adopter-facing reference). Four
  diagrams (Mermaid, GitHub-renders natively):
  1. Component diagram — OSTIARIUS (three/soon four entry points) → Petitio (Broker, today
     inert) → ARCA (router → real backend or stub) → the audit chain underneath all three.
  2. ARCA backend-selection precedence — the 3-level router as a decision tree.
  3. Petitio today vs. tomorrow — `Identity`/`requester` threaded but inert now; the shape a
     future `PolicyStore`/escalation flow would add, clearly marked "not built."
  4. Request/resolve sequence — requester → `check_injectable` (today: always proceeds) →
     backend → value injected at the boundary, never returned. Sets up (without building) the
     future diagram where step 2 can deny/escalate.
- **New `.github/ISSUE_TEMPLATE/adapter-request.yaml`** — per the marketplace research:
  `backend_name`, `auth_model` (keyless/federated preferred vs. static creds), `use_case`,
  `read_access_needed`, and a contribution checkbox block.
- **UI**: Project Explorer's backend picker gets the two-zone treatment (real: Local/GCP,
  clickable; stub: the five new kinds, flattened/monochrome, opening an explanatory modal
  instead of a config flow, never sharing a click target with the real options) — the
  marketplace research's strongest, most concrete finding, applied directly.

## 7. Risks

| Risk | Severity | Mitigation |
|---|---|---|
| A stub tile reads as "configured and protected" when it isn't | High (secrets-manager-specific safety issue, per marketplace research) | Two-zone layout, explanatory modal on click, never a shared click target with real backends |
| `SyncingBackend`'s offline fallback silently serves a very stale value indefinitely | Medium | `last_sync_result="stale-offline"` is a distinct, checkable value from `"fresh"`; `portunus_sync`'s report already surfaces per-reference status by name |
| A future reader mistakes Petitio's inert `requester` param for real enforcement | Medium | Explicit docstring + inline comment at the no-op point, "Petitio slice 0 of N" framing in docs |
| Teleport-shaped future expansion silently violates its embedding restriction | Low (not this pass, but worth recording) | Documented explicitly in §2 so it isn't re-discovered the hard way later |

## 8. Scale assessment

**Medium.** Five honest stubs (small, repetitive, low-risk), one real hardening to an existing
component, one inert parameter threaded through existing call sites (zero behavior change),
docs/diagrams, and a UI treatment. No new external dependency, no new real cloud/vault
integration, no enforcement-flip risk. Proceeding to stories.
