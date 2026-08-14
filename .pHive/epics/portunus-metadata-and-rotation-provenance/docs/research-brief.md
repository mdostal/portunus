# Research Brief — portunus-metadata-and-rotation-provenance

## Requirement

User asked directly: does the UI let you add metadata and create new secret entries, linking
them to where they come from — so that as real provider integrations land, the currently
greyed-out "Auto-rotate…" stub (added in portunus-swappable-trio) can become a real, automated,
end-to-end rotation button once everything's configured? Answer required auditing the actual
current UI code, not assuming from memory. Confirmed gap; user asked for epic docs to start now,
not urgent ("we'll get there").

## Current state — verified directly against the code

**`AddSecretForm.tsx` (the create flow, the one deliberate human-plaintext-entry point):**
captures exactly `name`, `sm_name`, `value`, `provider`, `project`, `env`, `tags` (a flat
`AddSecretDraft` interface, `types.ts:31-38`). Nothing else.

**`DetailDrawer.tsx`'s Move form (the edit flow, post-creation only):** captures `provider`,
`project`, `env`, `description`, `purpose`, `injected_as`, `group`, `related` — the richer
metadata set. Reachable only after a reference already exists; there is no creation-time path to
set these.

**Not exposed in the UI at all, either flow:** `kind`, `scope`, and `backend` (the per-reference
backend override shipped in portunus-vault-routing). CLI (`reg add`/`drop`/`retag --backend`)
and MCP (`portunus_drop`'s `backend` parameter) are the only ways to set these today.

**`provider` is a free-text tag, not a structured rotation-capable concept.** `Reference.provider`
(registry.py) exists purely as a `_STRUCTURED_TAG_FIELDS` entry used for tag-matching/display
(`resolve_by_tags`, `portunus_ask_preview`, the Console's provider filter). It answers "who
issued this" for a human reading the UI. It carries no information a program could act on to
actually rotate the credential — no endpoint, no account/auth context, no rotation API shape.
This is a structurally different concern from ARCA (where the *value* is stored) and Petitio
(who's allowed to *access* it) — call it, provisionally, **rotation provenance**: what it would
take for Portunus to actually call a provider's own API and mint a fresh value for a given
reference.

**The "Auto-rotate…" stub (portunus-swappable-trio, `DetailDrawer.tsx`)** is disabled, no route,
no handler — a placeholder for exactly this future capability. Its docstring/comment already
names the intent ("Placeholder for real key-rotation integrations... a separate, not-yet-built
direction"). This epic is the first real design pass at what that direction actually requires.

## What real rotation would need, concretely

Every provider's rotation API is genuinely different — this is not a single interface the way
`SecretBackend.access()` is:

- **Vercel**: project-scoped API tokens, rotated via the Vercel REST API (`POST /v2/user/tokens`
  to mint, then revoke the old one) — needs a Vercel account token with token-management scope.
- **GCP**: service-account keys can be rotated (`gcloud iam service-accounts keys create` +
  delete the old one) — Portunus already has WIF-based keyless auth for *accessing* GCP, but
  rotating a *stored* credential (e.g. a third-party API key living in GCP Secret Manager) is a
  different operation than anything `GcloudBackend` does today — that's calling the *third
  party's* rotation API, then storing the new value back via ARCA, not something GCP's own API
  does for you.
- **GitHub**: PAT rotation has no first-class API at all (fine-grained PATs can be revoked via
  API, but minting a new one programmatically is limited); GitHub Apps/installation tokens
  rotate automatically and don't fit this model the same way.
- **Stripe**: API key rotation (`POST /v1/api_keys` roll) exists but is account-scoped, not
  self-service per-key via a simple REST call in the same shape as the others.

**The pattern that emerges:** rotation is provider-specific integration work, not a generic
adapter interface like ARCA's `SecretBackend`. What Portunus *can* do generically, and what this
epic should actually scope, is the **provenance/config layer** that would let a future
provider-specific rotation integration know: which provider issued this reference, what
account/API context to use, and whether rotation is even supported for it yet — the same "real
vs. honest stub" posture ARCA already uses for backends, applied to rotation providers instead.

## What this means for scope

Two genuinely separable pieces, confirmed by the audit above:

1. **UI/metadata completeness** (small, mechanical, low-risk): unify `AddSecretForm` and
   `DetailDrawer`'s field sets so creation-time and edit-time expose the same complete metadata
   (including `kind`/`scope`/`backend`, currently CLI/MCP-only). This is a real, immediately
   useful gap-close regardless of what happens with rotation.
2. **Rotation provenance** (larger, genuinely new, provider-specific): a structured place to
   record "this reference is rotatable via provider X, using account/context Y" — and, later,
   real per-provider rotation adapters (Vercel, GCP, GitHub, Stripe, ...) analogous to how ARCA's
   real/stub backend split works. This epic scopes the provenance/config layer and the UI surface
   for it; it does NOT build any real rotation integration yet — that's provider-specific
   follow-on work, same posture as ARCA's honest stubs.

## Sources

Direct code inspection: `ui/app/components/AddSecretForm.tsx`, `ui/app/components/
DetailDrawer.tsx`, `ui/app/types.ts`, `src/portunus/registry.py` (`Reference`,
`_STRUCTURED_TAG_FIELDS`), `.pHive/epics/portunus-swappable-trio/` (the Auto-rotate stub's
origin and stated intent). Provider rotation-API shapes summarized from general knowledge of
each provider's public API surface — not independently re-verified via web search this pass;
flagged as a research gap for whichever provider gets picked for the first real rotation
integration.
