# Research Brief — portunus-swappable-trio

Produced by a 10-agent research workflow (6 vault-product researchers in parallel, one
RBAC/escalation-pattern researcher, one OSS adapter-marketplace UX researcher, one design
synthesis, one adversarial grill pass), run 2026-08-14. Full per-agent output and sources are
in the workflow's journal; this brief summarizes what's load-bearing for this epic's decisions.

## ARCA candidates — all six independently recommended stub-only

| Product | Self-host? | Auth model | License | Verdict |
|---|---|---|---|---|
| HashiCorp Vault / OpenBao | Yes, container-native (needs unseal lifecycle/KMS auto-unseal) | AppRole (role_id+secret_id) or cloud-IAM auth methods | Vault OSS: BUSL-1.1 (server-side only, doesn't block client code); OpenBao: MPL-2.0 fork | Stub. No operational Vault instance exists to validate against; ops burden (unseal) is real |
| Infisical | Yes, container-native (Postgres+Redis+app) | Universal Auth (client_id+client_secret → bearer token) | Core: MIT (open-core, Enterprise features separately licensed) | Stub. Cleanest license of the six; needs a real instance to build/test against |
| AWS Secrets Manager | No — cloud-only, zero self-host story | Keyless via STS `AssumeRoleWithWebIdentity` (already built, unused, in `auth.py` as `AWSWebIdentityAuth`) | Proprietary, $0.40/secret/mo + $0.05/10k calls | Stub. Cheapest to finish later (auth half already done) but no validated AWS account exists today |
| Doppler | No viable self-host (on-prem is sales-gated, undocumented) | Bearer token / OIDC Service Account Identity | CLI: Apache-2.0; core service: proprietary SaaS | Stub. Fails the no-hard-lock directive outright |
| 1Password Secrets Automation (Connect) | Yes, container-native, but gated behind a paid Business/Enterprise tenant to even bootstrap | Bearer token from a `1password-credentials.json` bootstrap file | Connect server: proprietary ToS, no OSS license; peripheral SDKs: MIT | Stub. No paid tenant provisioned; adds a two-container proxy tier |
| Azure Key Vault | No — cloud-only | Entra ID OAuth2; WIF-style federated credential possible but needs new Azure-side setup | Proprietary, ~$0.03/10k ops | Stub. No Azure tenant/App Registration exists; doesn't reuse GCP WIF work |

**Decision, confirmed by the operator:** local + GCP remain the only real backends this pass.
All six above ship as honest, interface-conformant stubs (`access()` raises `BackendError`
with a GitHub-request pointer) — no new real cloud/vault PoC. Revisit per-product if/when a
real validated environment exists (mirrors this project's own established pattern: GCP became
real because `personalsites-487021`/`ffe-cicd` are real, already-authenticated projects).

## Petitio — RBAC/escalation pattern research

Surveyed: HashiCorp Vault ACL/Identity, Teleport Access Requests, HashiCorp Boundary,
Kubernetes RBAC, generic PAM/JIT tools, CyberArk Secretless Broker. Closest structural match
to the operator's stated goal (approver acts, requester never sees the plaintext): Teleport's
request→review→time-boxed-grant workflow, layered on Secretless Broker's "the proxy injects,
the client never handles the secret" boundary — which Portunus's `injected_as` field already
half-implements.

Mapped onto Portunus's existing pieces: `Identity` (seeded the same way `AuditChain`'s actor
resolution already is), a future `PolicyStore` (reusing `Registry`'s JSON+flock+0600 idiom and
`Reference.matches_tag()`'s exact-match semantics rather than a new policy language), and a
future `EscalationRequest` state machine (`pending → approved | denied | expired`, "escalated"
modeled as routing metadata on a pending request, not a fifth state — per Teleport's own
shape, and simpler than Vault Sentinel/Boundary's scope hierarchy).

**Decision, confirmed by the operator:** none of PolicyStore/EscalationRequest ship this pass.
Petitio ships as a pure pass-through stub (`Identity` + an inert `requester` parameter) —
"it just passes through, no rush." The RBAC mapping above is preserved here as the target
shape for a future epic, not built now.

**Teleport itself is not a viable future embed-dependency**: OSS core relicensed to AGPLv3
(Dec 2023); compiled Community Edition binaries moved to a commercial license at v16 (2024)
that explicitly forbids reselling/embedding in another product, gated to companies under 100
employees/$10M revenue even for direct use. This conflicts with "bundled in some of my other
tools" — a future Petitio expansion should build its own request/escalation core (as the
operator already leaned toward) and treat Teleport as, at most, an optional external interop
under its real commercial terms, never an embedded dependency.

## OSS adapter-marketplace UX research

Surveyed: Terraform Registry (Official/Partner/Community), Airbyte (Certified/Community,
migrated new-connector-requests from raw Issues to Discussions to keep the bug queue clean),
dbt (Verified/Trusted/Community), Grafana (Labs-Core/Certified-Partner/Community, the
`3-data_source_request.yaml` Issue-form template is the standout precedent), HashiCorp Vault's
own Integrations page (same Official/Partner/Community tiering).

Converged pattern: (1) an honest, plainly-defined maturity tier per item; (2) unbuilt/stub
items stay *visible* but rendered inert — never clickable into a broken/misleading state,
which the research flags as a hard safety constraint specifically for a secrets manager (a
stub that looks configured could make a user believe a secret is protected when it isn't);
(3) "request a new one" is a short, structured form (GitHub Issue Forms YAML) asking for the
name, concrete use case, and whether the requester will help build it, routed separately from
the "use this" click path.

**Applied directly**: two-zone UI (real vs. stub, never sharing a click target), a new
`.github/ISSUE_TEMPLATE/adapter-request.yaml` per Grafana/Airbyte's field shape plus one
Portunus-specific field (`auth_model`: keyless/federated preferred vs. static creds — directly
load-bearing for this project's own keyless-first posture, not present in any surveyed
template).

## Sources

Full source lists (60+ citations across all six product researches, RBAC patterns, and
marketplace UX) are preserved in the workflow's journal (`workflow(run_id=wf_32b9f415-4dd)`,
per-agent `result` objects). Key ones cited directly above: HashiCorp's own license FAQ,
Vault/Infisical/Doppler/1Password/Azure official docs, Teleport's own licensing blog posts,
Grafana's/Airbyte's actual GitHub issue-template source files.
