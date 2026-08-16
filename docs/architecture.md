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

**Configuring this tree from the UI, not just the CLI** (portunus-bindings-settings-ui): the
Standalone UI's Project Explorer edits a `VaultBinding`'s full field set inline —
`backend`/`sync_mode` (already live before this epic) and, as of this epic, `account`/
`wif_audience` too, via `POST /api/bindings`. Both new fields are identity-selector/topology
strings, not credentials, so no new UI trust boundary is introduced — the write path simply
catches up to what `GET /api/bindings` already returned. A `RotationBinding`'s `account` (free-
text rotation context, e.g. a Vercel team slug) is likewise editable inline, in a reference's
detail view next to the Auto-rotate button (`POST /api/rotation-status`, new). Its `status`
(`stub`/`real`) stays derived from the real adapter registry and is structurally unreachable
from the UI — the handler never reads a `status` field off the request body at all, so a UI
control can never make a stub provider look real.

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

## 6. The desktop app is packaging, not a new component

Worth stating plainly: the Tauri desktop app (`ui/src-tauri/`) is **not** a fifth architecture
component alongside OSTIARIUS/ARCA/Petitio/audit. It's a native wrapper around the existing UI
API-routes entry point (§1's `UIRoutes` node) — a menu-bar presence and a self-update mechanism
around the same Next.js app `npm run dev` already serves, spawned as a sidecar process instead
of run manually. It introduces zero new request paths into OSTIARIUS, zero new backend logic,
and doesn't change how the CLI/MCP surfaces work — cross-project secret access already worked
before this existed, against the one shared `PORTUNUS_HOME`, regardless of which repo an agent
session happens to be rooted in. The one genuinely new piece of logic is the self-update
check, which shells out to the user's own already-authenticated `gh` CLI rather than embedding
a credential in the shipped app — the same "never hold a credential you don't have to"
posture this whole system already enforces everywhere else.

## 7. Provenance metadata: repo/source_files are OSTIARIUS-layer, not a new store

`repo` and `source_files` (`registry.py`) answer *which git repo, and which file in it, actually
consumes a secret* — a real gap found by inspecting the real ffe-cicd data: 342 references, one
shared GCP project spanning many repos, and nothing distinguishing which repo owned which
secret. This is registry metadata, the same layer `group`/`related`/`description` already live
on — it doesn't add a new component, doesn't change ARCA's backend-selection precedence (§2),
and doesn't touch how a value is resolved. `portunus tree --by repo` is the same tree-rendering
path as `--by group` (§1's `Resolver` node is unaffected), keyed on a different field.

## 8. Eager sync-down + vault export/import (portunus-vault-backup)

Two related but mechanically distinct additions on top of §2's ARCA backend-selection tree and
the local-encrypted vault's own file layout, both added to close a real gap: nothing purpose-
built existed for either "get a freshly-discovered secret's value cached locally right away" or
"move/restore the whole vault."

**Eager sync-down.** `discover --register` (CLI and MCP) previously left a freshly-registered
reference's local cache cold — `SyncingBackend` (§2) only pulls a value down on the *first real
resolve*, and a freshly-registered reference always lands at `state=requested`, which
`Broker.check_injectable` fails closed on. For a project bound `sync_mode=cached`, registration
now also calls `_eager_sync_down()` (`cli.py`), which warms the local cache immediately by
calling the routed backend's `access()` directly — deliberately bypassing `check_injectable` for
this one internal, value-never-returned cache-populate call. The reference's `state` stays
`requested` throughout: every real resolve/inject/ask/MCP path is gated exactly as before: only
*when* it's warm changes, never *whether* it's injectable.

**Vault export/import.** `portunus vault export`/`portunus vault import` (CLI-only — no MCP
tool, no UI surface: a full-vault archive should never be triggerable by an LLM-facing tool
without a human directly initiating it) move the vault's critical-state surface as one portable,
passphrase-locked archive. Two pieces make this safe, both in `backup.py`:

- **Coordinated snapshot.** `registry.json`, `master.key`, `vault.enc.json`,
  `vault-bindings.json`, and `audit.log` + `.clock` (the append() sequence counter — MUST travel
  with `audit.log`, not just alongside it, or a restored chain's next `append()` can re-mint a
  seq that already exists) are all read together under every relevant lock — `registry.lock`,
  `vault.enc.lock`, `vault-bindings.lock` (new; previously the only critical-state file with no
  lock at all), `.clock.lock` — acquired in one fixed, alphabetically-sorted order so the result
  reflects one consistent instant, never independent reads straddling a concurrent writer.
  Legacy `gcp-bindings.json`/`rotation-bindings.json` are included if present, read unlocked
  (neither has a dedicated writer-side lock today either).
- **Passphrase re-encryption.** The snapshot is bundled and re-encrypted under a PBKDF2-SHA256-
  derived key (600k iterations) from an operator passphrase — never the vault's own `master.key`
  bundled as-is. `master.key` alone is sufficient to decrypt every stored value; an archive
  meant to leave the access-controlled `PORTUNUS_HOME` must not carry a live decryption key
  un-re-encrypted. The passphrase itself is sourced only via `PORTUNUS_EXPORT_PASSPHRASE` or an
  interactive prompt, never an inline flag — the same boundary-only convention `portunus drop`
  uses for its own sensitive input.

No bidirectional multi-machine sync was built — `SyncingBackend` already solves the narrower
"stay usable while disconnected" problem it exists for, and real sync would need genuine
conflict-resolution design given this vault has real concurrent-writer traffic (see the
`AuditChain`/`LocalEncryptedBackend` lock fixes this epic's own prerequisite work built on).

## 9. `org` hierarchy, sub-vault navigation, and custom views (portunus-vault-trust-and-access)

Planned with a heavier horizontal/vertical process (`.pHive/epics/portunus-vault-trust-and-
access/docs/`) rather than the lighter research-brief-only shape recent epics defaulted to —
the ask (metadata quality, an organizational hierarchy, stubbed RBAC, onboarding) was
genuinely large and interdependent enough to warrant it, the same process
`portunus-standalone-core` used for the original registry/adapter/UI buildout.

**`org`, one level above `project`.** `Reference` gains `org: str = ""` — the same flat-
structured-tag pattern `provider`/`project`/`env`/`repo` already use, added to
`_STRUCTURED_TAG_FIELDS` (so `find --tags org=...` and `retag()`'s collision check both pick it
up immediately) rather than a new nested hierarchy object. `VaultBinding` + `project` already
gave "each vault is its own thing" (per-project backend/credential, already proven for GCP
multi-account); `org` fills the one real missing rung — grouping several projects under one
organizational umbrella (e.g. `firefly-events` spanning `ffe-cicd`/`shindig`).

**Sub-vault navigation is a UI concept, not a new store.** The Standalone UI's Vault Map
renders an org → project drill-down over the `org`/`project` fields — no new backend, no new
API call, no new permission boundary (that's explicitly deferred, stubbed-only future work).
Drilling into a project *feels* like its own small vault (its own list, its own completeness
summary) without duplicating any state.

**Custom views** (`views.py`, `PORTUNUS_HOME/views.json`) are deliberately orthogonal to that
structural hierarchy — a named, human-curated list of reference names for task-shaped
clustering ("everything for the Shindig deploy") that doesn't map onto org/project/env.
Every mutator wraps its own load→mutate→save inside one `flock` acquisition from the start —
unlike `vault-bindings.json`'s own retrofitted save-only lock (§8), this store never had the
race in the first place, proven with a real multi-process concurrent-write test.

**Missing-metadata signal** (`ui/app/completeness.ts`) is a pure, derived-on-render function
over fields the UI already fetches — no new stored field, nothing to drift out of sync with
what it's computed from.

**Role/policy schema — built as a stub, deliberately not enforced.** `roles.py` persists
`PolicyRecord(scope_type: org|project|env, scope_value, role, actions[])` to `PORTUNUS_HOME/
roles.json` for real — `portunus roles set/delete/show` and the Settings page both genuinely
read and write it — but `check_injectable()`/`retag()` never consume it.
`tests/test_roles.py::test_check_injectable_and_retag_are_byte_identical_with_or_without_
roles_configured` is the defining test: byte-identical behavior with or without policies
configured, not just "defaults to permissive" (a materially weaker guarantee a future edit
could silently erode). Building on `Identity`/`check_injectable`'s already-inert `requester`
seam (§3) in SHAPE, extended to hierarchy-scoped actions — the evaluation function itself
(most-specific-scope-wins? explicit deny beats allow?) is real, unresolved future work.

**LLM-suggests, human-confirms metadata.** `Reference.suggested` is a sidecar dict
(`{field_name: {value, by, at}}`), written only by `Registry.suggest_metadata()` (and the MCP
tool of the same name) — restricted to `SUGGESTIBLE_FIELDS` (`description`/`purpose`/`tags`/
`group`). Routing fields are structurally rejected: `suggest_metadata()` raises rather than
silently ignoring them, so a caller never mistakes "was rejected" for "was suggested but not
shown yet." Confirming a suggestion is NOT a new mutation path — it's the existing `retag()`
applying the suggested value, then `clear_suggestion()` drops the sidecar entry; rejecting is
just `clear_suggestion()` with the live field never touched. This keeps `retag()` as the ONLY
code path that ever writes the four suggestible fields, whether a human typed the value or
confirmed an agent's proposal.

**Settings, the setup wizard, and About are UI-only additions** with no new backend concepts
beyond what's already described above: Settings surfaces the org/project hierarchy (read-only
summary) and roles.json (genuinely editable, always visibly labeled "not yet enforced");
`SetupWizard` is a first-run-only flow (`portunus vault status` detects an uninitialized
`PORTUNUS_HOME` — absence of BOTH `registry.json` and `vault-bindings.json`) that walks through
backend choice and an in-UI trigger for `gcloud auth login` (the real, unmodified gcloud OAuth
flow — the wizard doesn't reimplement or intercept it, just gives it a button); its own Roles
step is literally disabled (`<fieldset disabled>`), a deliberately different treatment from
Settings' editable-but-labeled stub, since a first-run flow isn't the place to risk someone
getting lost configuring permissions before they have a vault at all.

## 10. Metadata crawl + deploy-docs report (portunus-metadata-crawl)

Follow-on to §9's suggest/confirm workflow — the "bulk-suggest" work that epic's own
design-discussion.md explicitly deferred. Two independent, read-only tools, neither of which
calls an LLM or writes a `Reference` field itself.

**`crawl_candidates()` (`src/portunus/crawl.py`) is a discovery bundler, not a writer.** For
every reference missing description/purpose/org, it bundles everything already known —
`sm_name`, `group`, `project`, `org`, `repo`, `source_files`, its project's `VaultBinding`, its
provider's `RotationBinding` — into one JSON object, for an LLM (Claude Code, another
MCP-connected agent, or a human) to read and act on via the already-shipped
`Registry.suggest_metadata()`/`portunus_suggest_metadata` (§9). Portunus has zero LLM-API-key
infrastructure anywhere, deliberately — this design bundles context for an *external* caller
rather than inventing one. Real vault data checked during planning (393 references) showed
`repo` set on fewer than 1% of references — a repo-cloning crawler would have almost nothing
to scan — while `sm_name` (often the literal env var name) and `group` (91% filled) are the
strongest signals actually available today; real external-repo scanning stays out of scope
until repo fill-rate rises. Exposed as `portunus crawl [--org] [--project] [--json]` and the
`portunus_crawl_candidates` MCP tool.

**`generate_report()` renders current vault state as Markdown** — an org → project structure,
each reference's known metadata, and an explicit `## Gaps` section — independent of whether
`crawl_candidates()` ever found or fixed anything. Useful immediately as a real "deploy docs"
starting point (the user's own framing, for a company with no documentation of what's using
which credential). Exposed as `portunus report [--org] [--project] [--out path]`.

**The Settings UI surfaces both as thin shells, never an automatic filler.** `/api/crawl` and
`/api/report` shell out to the same CLI commands every other route already uses
(`runPortunus`) — no second implementation. The Settings page's "Crawl & report" section
reuses `completeness.ts`'s existing derivation (§9) to count references missing metadata, adds
a "Fetch crawl bundle" button (framed explicitly as context for an LLM session to read, not an
auto-fill), and a "Download report" button. Confirming any metadata a human or LLM proposes
from the bundle still goes through the existing `portunus metadata confirm` flow (§9) — this
epic adds no new write path.

## 11. Leak detection across logs/.claude/local files (portunus-leak-scan)

Detects whether a managed secret's actual decrypted value shows up somewhere it shouldn't --
logs, `.claude` conversation transcripts, shell history, or any other explicitly-configured
local path. Advisory only: proven, not just asserted, that `check_injectable()`/`resolve()`
behave byte-identically whether or not a reference has active leak findings, mirroring §3's
roles.json precedent.

**The strictest instance of the secret-boundary-invariant in the codebase.** Every other module
that touches a decrypted value either hands it only to a boundary sink (`resolver.py`) or never
calls `Backend.access()` at all (`crawl.py`, §10). `leakscan.py` is a new third shape: it MUST
call `.access()` to get values to search FOR, then guarantee those values never escape beyond an
in-memory per-line comparison. Values live only in a local dict inside the scan function's own
stack frame; `Finding(ref_name, path, line_number, byte_offset)` has no field capable of holding
a value; a forced mid-scan exception's message is verified never to contain the searched value.

**Line-based, incremental scanning.** Every configured file is scanned line-by-line (free line
numbers, no chunk-boundary-match bugs) with a per-file `Watermark` (byte offset + a
`(size, mtime)` fingerprint + consumed line count) so a re-scan only reads newly-appended bytes.
A shrunk/replaced file is rescanned from byte 0. Values shorter than
`MIN_SEARCHABLE_VALUE_LENGTH` (8) are never fetched into the search set — a trivial-length value
would false-positive-match constantly. A single compiled multi-pattern alternation does one
linear pass per file, not one pass per secret — real scale data checked during planning
(3.4 GB / 4,421 files under one `~/.claude` alone) ruled out anything less.

**Three separate locked JSON stores**, deliberately not one: `leak-scan-config.json` (scan-path
globs, empty by default — never auto-populated), `leak-status.json` (findings + escalation
state, rewritten on new findings/rotations), `leak-scan-watermarks.json` (rewritten on every
scan, the highest-churn of the three). Sharing one lock across all three would serialize a
frequent cheap watermark update behind a lock a rare config-edit also wants, for no benefit.

**Escalation is derived, not stored.** Severity (`warn`/`urgent`/`critical`) is computed at read
time from elapsed time since the EARLIEST `first_detected_at` across a reference's findings
(0–2d / 3–6d / 7+d) — never persisted redundantly. `portunus leak mark-rotated <name>` is an
explicit, documented human assertion Portunus cannot independently verify; it also invalidates
the watermark for every file where that reference had a finding, so a genuinely premature
mark-rotated gets caught by the next scan rather than silently protected by a watermark that
already scanned past the still-leaked bytes — a real gap the epic's own live-proof pass against
the actual Settings page caught and fixed before shipping.

**MCP surface: full parity with the CLI, by explicit user decision.** `portunus_leak_status`
exposes only already-computed severity/finding-count/timestamps and still never triggers a
scan. `portunus_run_leak_scan`, `portunus_leak_scan_config_show/add_path/remove_path`, and
`portunus_leak_mark_rotated` mirror the CLI 1:1, and were added AFTER the epic initially shipped
`portunus_leak_status` as the only MCP surface, deliberately keeping scan-triggering CLI/UI-only
(an agent triggering reads of a user's own conversation history at its own initiative was judged
a materially different trust boundary than reading metadata the vault already had). The user
explicitly revisited that tradeoff and chose to widen it — recorded, not silently reversed, in
`.pHive/epics/portunus-leak-scan/docs/design-discussion.md` §2's addendum. What did NOT change:
the human-configured scan-path set is still the only thing an agent can ever cause Portunus to
read — `portunus_run_leak_scan` takes no path argument, so widening WHO can trigger a scan never
widened WHAT can be scanned. Every new tool is structurally verified to never decode file
content, call `.access(`, or open a file directly — they only ever call the same
`leakscan.py` functions the CLI itself uses.

**A detective control, not a preventive one**, said explicitly in every surface's own copy
(CLI help text, Settings section, README). This finds secrets that already leaked; it does
nothing to stop the next paste into a chat window. The standing project policy (never act on a
credential pasted in chat; flag it and ask the user to rotate) remains the actual first line of
defense — this epic is a safety net under it, not a replacement.

## 12. Container deployment: CLI + MCP server, same-pod/same-host reachability (portunus-container-image)

The `Dockerfile` at the repo root packages the CLI + MCP server only — not the Next.js UI/desktop
app, which is a human-facing dashboard, not something you'd run as a k8s sidecar (§6 already
established the desktop app is packaging, not a new component; this section makes the same call
for the container image's scope).

**Targets same-pod/same-host reachability, not a network-shared broker service.** The MCP server
is stdio-only today (`mcp_server.py::main` calls `mcp.run()` with no transport argument, even
though the underlying `mcp` library already supports `sse`/`streamable-http`). Rather than treat
that as a gap to close immediately, the container epic treats it as the natural v1 boundary: a
consumer reaches Portunus via `docker exec`/`kubectl exec`, a shared pod volume, or by having
Portunus itself start the consumer (`resolve --exec <command>`, already the exact pattern local
CLI usage has always used). A genuinely network-reachable shared service — one Portunus instance
many pods call over the network — would need the currently-stub-only RBAC (`roles.py`, §9) to
actually be enforced; that's real, separate, larger future work, not silently assumed here.

**Non-root, VOLUME-declared `PORTUNUS_HOME`.** The container runs as a dedicated non-root user —
`PORTUNUS_HOME`'s own 0600/0700 file permissions are already the real access control, so running
as root inside the container adds no benefit and is an avoidable hardening gap for a
secret-handling image. A real bug the epic's own live-proof pass caught before shipping: a fresh
Docker named volume defaults to root:root ownership, which broke writes for the non-root user on
first use. Fixed by creating and `chown`-ing `PORTUNUS_HOME` in the image BEFORE both the
`VOLUME` instruction and the `USER` switch — Docker initializes an empty named/anonymous volume
by copying the image's existing content/ownership at that mount path, so this is what makes the
volume writable by the right user from first use.

**The persistent-volume requirement is a real, documented hazard, not a footnote.**
`LocalEncryptedBackend`'s master key self-bootstraps (no secret-zero problem — no human
passphrase needed for normal operation), but that same self-bootstrapping means an unmounted or
removed volume silently generates a NEW key on next start, making every previously-stored value
permanently unrecoverable. This risk is specific to the local-encrypted backend; GCP-backend-only
usage has no local ciphertext to lose.

**One image, `gcloud` CLI included, not a slim/full split.** `GcloudBackend` shells out to the
real `gcloud` binary (confirmed: no `google-cloud-*` Python SDK dependency in `pyproject.toml`),
so GCP backend support requires the CLI baked into the image. A second, smaller local-only image
is real future polish, deliberately not built now — image size matters little for a sidecar/CI
image, and two images is ongoing maintenance cost for an early, unproven feature.

**Auth is already solved per backend, not newly invented.** Local-encrypted: zero-config. GCP
local dev: mount the developer's own `~/.config/gcloud` read-only, reusing today's exact ambient
`gcloud` auth behavior. GCP real Kubernetes: GKE Workload Identity — already keyless (§ auth.py's
`GCPWorkloadIdentityAuth`), the recommended production path, not a new capability this epic had
to build.

## See also

- [README.md](../README.md) — component model table, install/usage, MCP tool reference
- `.pHive/CONTEXT.md` — terminology glossary
- `.pHive/epics/portunus-swappable-trio/docs/` — the research and design record behind this
  page (6-product vault-adapter research, RBAC/escalation-pattern research, OSS
  adapter-marketplace UX research)
