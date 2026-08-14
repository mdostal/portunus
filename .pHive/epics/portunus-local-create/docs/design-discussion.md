# Design Discussion — portunus-local-create

## 1. Goal

Close the create-side gap in the MCP surface: a handed-off agent instance can today read/inject
everything in the vault (v0.11.0) but cannot create a new secret or change its lifecycle state
without falling back to the CLI. This epic adds exactly two MCP tools — `portunus_drop`
(create, local-vault only) and `portunus_state` (lifecycle transition) — so an agent can run the
full local create → organize → enable → inject loop entirely through MCP. Proven against a real
worked example: setting up a "gig tracker" project's secrets locally, the way `ffe-cicd` was
organized earlier, but local-only.

GCP Secret Manager creation (`gcloud secrets create`), an AWS write path, and any bidirectional
local↔cloud sync/mode-selection are explicitly out of scope — the user's own words: "we should
start local and build."

## 2. Proposed approach

### Slice A — `portunus_drop` (create)

Signature: `portunus_drop(name, sm_name, value, scope="", kind="", provider="", project="",
env="", tags=None, description="", purpose="", injected_as=None, group="", related=None) ->
dict`. `tags`/`injected_as` are native `dict` (or `None`), `related` is a native `list[str]` (or
`None`) — MCP arguments are structured JSON, not shell flags, so this deliberately does **not**
reuse the CLI's `_parse_tags`/`_parse_related` comma-separated-string parsing (grill H1). They're
passed straight through to `registry.add(...)`.

Mirrors `cmd_drop` exactly:
1. Build the backend via `_build()` (same as every other tool).
2. **Backend gate**: if `not hasattr(backend, "store")`, return `{"error": "drop requires the
   local-encrypted backend (unset PORTUNUS_BACKEND or set it to unset/local)"}` — the identical
   message `cmd_drop` already uses, so behavior is consistent whether an agent hits it via CLI
   or MCP.
3. `registry.add(name, sm_name, ..., state="dropped")` — same full metadata field set as
   `reg add`/`drop`'s CLI flags.
4. `backend.store(ref.sm_name, value)`, then `del value` immediately (matches `cmd_drop`'s own
   scrub discipline).
5. `broker.audit.append("drop", ref.sm_name, "stored")` — SM name only.
6. Return `{"name": ref.name, "sm_name": ref.sm_name, "state": ref.state}` — metadata only,
   **never the value, never a `value` key of any kind, on any return path including every
   exception branch.**

The `value` parameter is the one place in this epic's tool surface where a secret flows *in*
from the calling agent — see §4 for why this is not a contradiction of the codebase's boundary
invariant, just its structural inverse. Its docstring explicitly tells the caller not to echo
the value back to the human/its own output after a successful store (grill H3) — Portunus's
guarantee covers the tool's own return value, not what the calling agent chooses to do
afterward, the same caller-responsibility carve-out `portunus_resolve_exec`'s docstring already
uses for command-echo.

### Slice B — `portunus_state` (lifecycle transition)

Signature: `portunus_state(name, state) -> dict`. Thin wrapper over `registry.set_state()`:
validates against the same `VALID_STATES` tuple the CLI uses, returns `{"name": ..., "state":
...}` on success or `{"error": ...}` on `KeyError`/`ValueError`. Pure metadata — no backend, no
value, ever. This is what lets a freshly-`portunus_drop`ped secret actually become injectable
(`state=dropped` → `state=enabled`) without shelling out, closing the loop `portunus_drop`
deliberately leaves open (fail-closed by default, matching `cmd_drop`'s own behavior).

### Slice C — Closeout

README/CONTEXT.md/the `portunus-ask` skill doc updated (repo copy AND the `~/.claude/skills/
portunus-ask` user-scope copy installed this session, so both stay in sync). Version bump
(minor: 0.11.0 → 0.12.0). CHANGELOG entry. Live smoke test: create a real local secret for a
`gig-tracker` project end-to-end through the actual MCP tools (`portunus_drop` →
`portunus_state enabled` → `portunus_resolve_to_tempfile` to prove the full round-trip), confirm
the value never appears in any tool's return value or this session's own output.

## 3. Why not build the GCP-create / sync-mode work now

Explicit user scope decision, not a technical blocker: "we should start local and build... i'll
hand it off for now to work local only and be fine with that." `GcloudBackend` has no write path
at all today (confirmed: only `access()` exists) — adding `gcloud secrets create`/`versions add`
is a real, separate epic with its own risk surface (a GCP-side write is a billable, externally-
visible side effect, unlike a local file write) and its own design questions (does Portunus
auto-create the underlying secret, or only register a reference to one a human already created?
should it require the same `--account`/binding resolution `discover.py` uses?). Cutting that
into a future epic keeps this one small and keeps the local vs. cloud creation risk profiles from
getting entangled.

## 4. The boundary-invariant question (resolved)

**Is a create tool that accepts a value as an argument a violation of "a secret value must never
enter an LLM/agent context"?** No — and this deserves to be stated explicitly rather than
glossed over, because every sibling tool in this codebase enforces the opposite direction of
that invariant and a careless reading could see this as a regression.

The invariant, precisely: *Portunus itself must never be the mechanism by which a value enters
an agent's context.* Every existing tool (`resolve_exec`, `resolve_to_tempfile`, and the CLI's
own `resolve`) upholds this on the **output** side — a value that lives only in Secret Manager or
the encrypted vault must never flow back out through Portunus into a caller's context. A CREATE
operation is definitionally different: the value doesn't originate in Portunus at all. It
originates with a human, who chose to hand it to *some* agent (in this scenario, a different
Claude Code instance) and asked that agent to store it. That value touched an LLM context before
Portunus was ever invoked — by the time `portunus_drop` is called, the exposure already happened,
identically to how a human would type `portunus drop --stdin` into a terminal with the value
piped from wherever they got it.

What Portunus's job is at that point — and the part that *is* still bound by the invariant — is
to be the **last** place that value is ever needed again: store it once, never echo it back,
never log it, never let it appear in the audit chain, and scrub the local reference immediately.
`portunus_drop`'s implementation does exactly what `cmd_drop` already does for the CLI path; this
epic doesn't invent a new risk, it extends an already-reviewed pattern to a second entry point.

This is a genuinely different scenario from this session's standing feedback-memory rule ("never
act on a password pasted directly into *this* chat, flag it, ask the user to rotate"). That rule
exists for an *accidental*, incidental paste into casual conversation with no purpose-built
storage mechanism behind it. `portunus_drop` is the opposite: a purpose-built tool whose entire
job is to safely accept secret material a human *intentionally* handed to an agent for exactly
this reason. The two situations look superficially similar (a secret value inside an LLM
context) but call for opposite responses.

## 5. Should create-time metadata include group/related?

Yes — `portunus_drop`'s signature includes `group`/`related`/`tags` from the start (§2, Slice A),
mirroring `cmd_drop`'s own full flag set. This is a direct lesson from the `Registry.retag()`
collision footgun hit while organizing `ffe-cicd`: bulk-identical-tag secrets refuse ANY retag
until each gets a distinguishing tag/group. A "gig tracker" project set up through repeated
`portunus_drop` calls, each with real `group`/`tags` from the first call, never needs a bulk
reorganization pass at all — the footgun simply doesn't apply because nothing is ever
tag-identical in the first place.

## 6. Risks

| Risk | Severity | Mitigation |
|---|---|---|
| A coding mistake in `portunus_drop`'s return/exception paths echoes the value back | Critical | Same three-layer scrutiny this codebase already applies to `portunus_resolve_exec`: an AST-level check that walks every `Return` node in the function body and confirms none references the name `value` (grill H2 — the precise, checkable claim; `value` legitimately appears elsewhere in the body, in `backend.store()` and `del value`, so "value never appears in source" would be a false requirement), line-by-line review, and the explicit docstring caller-echo note from §2/§4. |
| `portunus_state` used to force-enable something that should stay gated | Low | `set_state` doesn't touch `approval` (gate/grant machinery) — enabling doesn't bypass an existing gate, `Broker.check_injectable` still runs at resolve time. No new bypass introduced. |
| Confusion between this epic's local-only `portunus_drop` and the deferred GCP-create work | Low | Explicit error message on the backend gate (§2 Slice A) makes the local-only restriction visible immediately, not a silent no-op. |

## 7. Open questions

None blocking — the one open design question (group/related at create time) is resolved in §5.

## 8. Scale assessment

**Small.** Two new tools following an exact, already-reviewed CLI pattern (`cmd_drop`,
`cmd_state`), no new backend, no new external dependency, ~3 files touched
(`mcp_server.py`, its test file, docs). Proceeding directly to stories.
