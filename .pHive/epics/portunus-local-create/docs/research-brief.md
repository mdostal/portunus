# Research Brief — portunus-local-create

## Requirement

Add local-only secret CREATION to Portunus, exposed through the MCP server so a handed-off
Claude Code instance (or any other MCP-capable agent/harness) can create and organize new
secrets in the local vault, not just read/inject what's already there. Motivating scenario: the
user wants to hand this Portunus instance to other agent instances and have them set up a new
personal project ("gig tracker") locally, the way `ffe-cicd`'s 342 secrets got organized into
groups earlier this session — but starting local, not GCP, per explicit user scope decision.

## What already exists

**`portunus drop` (cli.py:556-599, `cmd_drop`) is the one plaintext-entry point today.** It:
- Accepts a value via `--stdin` or `--value-file` only — never an inline argv flag (the CLI's
  own docstring: "the value never enters the LLM chat, ~/.claude, or a provider").
- Refuses outright if the active backend isn't local: `if not hasattr(backend, "store"):
  return _err("drop requires the local-encrypted backend...")`. `GcloudBackend`/
  `AWSSecretsManagerBackend` have no `store()` method at all (confirmed by grep — only
  `LocalEncryptedBackend.store()` exists, localvault.py:80).
- Calls `registry.add(...)` with the full metadata field set (scope, kind, provider, project,
  env, tags, description, purpose, injected_as, group, related) then `backend.store(sm_name,
  value)`, `del value` immediately, and `broker.audit.append("drop", ref.sm_name, "stored")` —
  SM name only, never the value, into the audit chain.
- Lands the new reference at `state="dropped"` (fail-closed) — a separate `portunus state <name>
  enabled` call is required before it's actually injectable. This state transition
  (`registry.set_state`, registry.py:184-190) is pure metadata: validates against
  `VALID_STATES = ("enabled", "locked", "dropped", "revoked", "requested")`, raises `ValueError`
  on an invalid state or `KeyError` on an unknown reference. No value ever touches it.

**No MCP tool exists for either operation.** All 8 tools shipped in portunus-mcp-server (v0.11.0,
mcp_server.py) are read/inject-only: `portunus_health/list/tree/ask_preview/bindings_show/
discover/resolve_to_tempfile/resolve_exec`. An agent connected only via MCP today has no way to
create a secret or change its lifecycle state — it would have to fall back to shelling out to
the CLI directly, defeating the point of the MCP surface.

**Naming convention is established and consistent**: every existing tool mirrors its CLI
command 1:1 (`portunus_list` ↔ `list`, `portunus_tree` ↔ `tree`, `portunus_discover` ↔
`discover`, `portunus_bindings_show` ↔ `bindings show`). A create tool should be `portunus_drop`
and a state tool should be `portunus_state`, matching `portunus drop`/`portunus state` exactly.

**`Registry.retag()`'s known footgun** (documented in project memory from the portunus-secret-
tree epic): the collision check evaluates a reference's entire current tag-identity
(provider+project+env+tags), not just the field being changed — so bulk-identical-tag secrets
refuse ANY retag until each gets a distinguishing tag. This does not affect `registry.add()`
(creation, not retagging) directly, but it means a "gig tracker" project's secrets should get
real distinguishing metadata (at minimum a unique `tags` entry, ideally `group`) at CREATE time
— setting it later via bulk retag will hit the exact same footgun that bit ffe-cicd's grouping
pass.

## The dominant design question

Every existing MCP tool in this codebase enforces one invariant: **a secret value must never
flow back to an agent's context** (resolver.py's module docstring; portunus_resolve_exec/
portunus_resolve_to_tempfile never return a value on any path). A CREATE tool inverts this: the
value must flow **in** as a tool argument, because the calling agent is the one who currently
holds it — a human handed a brand-new key to that other Claude instance and asked it to store it
in Portunus. Portunus cannot prevent the value from having already touched that other agent's
context before the tool call; that's structurally inherent to "hand Portunus to another instance
so it can create keys," not a gap in this codebase's design. What Portunus *can* still guarantee
on the way out: the value is never echoed back in the tool's own return, never logged, never
appears in the audit chain, and any local variable holding it is scrubbed promptly — the same
`del value` discipline `cmd_drop` already uses. This is architecturally the same "no value on
the way out" invariant every prior tool honors; it just doesn't (can't) apply to the way in.

## Validation confidence

Codebase-only — no new third-party library, `mcp` SDK usage identical to the 8 existing tools.
No external API/SDK surface to validate against context7; this is pure internal reuse of
`Registry.add()`, `LocalEncryptedBackend.store()`, and `Registry.set_state()`, all already
covered by the existing unit-test suite.

## inconsistency_risk_signals

- The "value must never flow back" invariant, if copy-pasted uncritically into this epic's
  design doc, would read as a contradiction with a create tool accepting a value as input. Grill
  should confirm this is a real, resolved distinction (documented above) rather than an
  unexamined tension.
- `portunus_drop` as a tool name could be confused with the registry-removal `reg rm`/`drop`
  overload risk — worth double-checking no naming collision exists among the 8 existing tools
  (confirmed: none do).
