# MCP Server & Agent Integration Guide

Portunus exposes a [Model Context Protocol](https://modelcontextprotocol.io/) (MCP) stdio server alongside the CLI and the web UI. Other agents and harnesses — not just the local Claude Code session — can use it to query metadata, resolve secrets at the boundary, and manage vault state without ever seeing a secret value in a tool result.

## Security model

**Tool results never contain a secret value.** This is a structural guarantee, not a convention:

- Discovery and list tools return metadata only (name, description, state, tags).
- Resolution tools return a **file path** (`portunus_resolve_to_tempfile`) or a **command result** (`portunus_resolve_exec`). The value flows into the command's argv or into a 0600 temp file — it never passes through the tool's return value.
- `portunus_drop` is the one tool where a value flows *in* (storing a new secret). The return value is metadata only; the value is never echoed back.

This means an LLM context reading a tool result cannot inadvertently capture or relay a secret.

## Starting the server

```sh
portunus mcp
```

This starts the stdio MCP server. You do not normally run this directly — use `portunus agent init` (see below) to wire it into a harness automatically.

## `portunus agent init`

The single-command onboarding path. Detects every supported agent CLI on the machine and registers the MCP server + installs usage skills.

```sh
portunus agent init
```

Supported harnesses today: **Claude Code**, **Codex CLI**.

**What it does:**

- Registers the MCP server in each harness's MCP config (e.g. Claude Code's `mcpServers` in `settings.json`).
- Installs Portunus-specific skills (e.g. `portunus-ask`, `portunus-drop`) into the harness's skills directory so agents can invoke them by name.
- Idempotent — safe to re-run any time, including after installing a new harness.

**Limit to one harness:**

```sh
portunus agent init --harness claude
portunus agent init --harness codex
```

## `portunus agent status`

Check what's currently wired — never mutates anything.

```sh
portunus agent status
```

Reports which harnesses are present, which have the MCP server registered, and which usage skills are installed.

## Manual wiring

For harnesses not yet supported by `agent init`, configure the MCP server directly. The transport is `stdio`; the command is:

```
portunus mcp
```

Example MCP client config (generic JSON form):

```json
{
  "mcpServers": {
    "portunus": {
      "command": "portunus",
      "args": ["mcp"]
    }
  }
}
```

If `portunus` is not on `PATH` in the harness's environment, use the full path (e.g. from `which portunus`).

## MCP tools

All 23 tools are listed below. No tool ever returns a resolved secret value.

### Liveness

| Tool | Description |
|---|---|
| `portunus_health` | Liveness check. Returns `"ok"`. Does not touch the registry or vault. |

### List / search / metadata

| Tool | Key parameters | Description |
|---|---|---|
| `portunus_list` | `project: str` | List every reference for a project — metadata only. Use this before calling resolve to find the exact reference name. |
| `portunus_search` | `query: str`, `project?`, `provider?`, `env?`, `state?` | Free-text search across name, sm_name, description, purpose, tags, and group. Returns zero to many results; empty string params mean no filter. |
| `portunus_tree` | `project?: str`, `by?: "group"\|"repo"` | Render secrets by group hierarchy + related links — `by="group"` (default) or `by="repo"`. Same shape as `portunus tree --json`. |
| `portunus_ask_preview` | `request: str` | Preview what a natural-language request resolves to — metadata only, never injects. Handles fetch requests only; rejects add/rotate/list requests with a clear error. |
| `portunus_crawl_candidates` | `org?: str`, `project?: str` | Bundle context for references missing description/purpose/org. Read this, then call `portunus_suggest_metadata` for any fields you have a real proposal for. |
| `portunus_suggest_metadata` | `name: str`, `description?`, `purpose?`, `group?`, `tags?: dict` | Propose metadata fields — lands in a provenance sidecar, **never the live field**. A human must confirm via `portunus metadata confirm` or the UI before it takes effect. |

### Resolve / inject

| Tool | Key parameters | Description |
|---|---|---|
| `portunus_resolve_to_tempfile` | `name?: str`, `tags?: dict` | Resolve one secret to a 0600 temp file; returns `{"path": "..."}`. Address by exact `name` or by `tags` — give one, not both. Treat the path as a pointer to hand to another tool; do not read the file back into your own response. |
| `portunus_resolve_exec` | `argv: List[str]`, `name?: str`, `tags?: dict` | Run a command with one secret substituted in; returns `{"stdout", "stderr", "returncode"}`. Write the literal marker `{{secret}}` in `argv` where the value should go, e.g. `["curl", "-H", "x-api-key: {{secret}}", "https://..."]`. A non-zero returncode is returned, not raised. |

Both tools accept dual addressing: exact `name` (from a prior list/search/preview call) or `tags` dict.

### Drop / state

| Tool | Key parameters | Description |
|---|---|---|
| `portunus_drop` | `name`, `sm_name`, `value`, `provider?`, `project?`, `env?`, `tags?`, … | Create a new local-vault secret. Lands at `state="dropped"`; call `portunus_state(name, "enabled")` to make it injectable. `value` is the one place a secret flows **in** — never echoed back. |
| `portunus_drop_bulk` | `entries: List[dict]` | Create many secrets at once. Each entry takes the same fields as `portunus_drop`. Returns `{"created": [names], "failed": [{"name", "error"}]}`. |
| `portunus_state` | `name: str`, `state: str` | Change a reference's lifecycle state. Valid states: `enabled`, `locked`, `dropped`, `revoked`, `requested`. |

### Access control & bindings

| Tool | Key parameters | Description |
|---|---|---|
| `portunus_bindings_show` | `project?: str` | Show per-project vault bindings (backend, sync_mode, account, WIF audience). |
| `portunus_rotation_status` | `provider?: str` | Show per-provider rotation bindings (status, account, capability). |
| `portunus_rotation_audit` | — | Inventory all references and OAuth credentials by provider; report auto/manual/unknown capability with reference names and superseded key IDs. |

### GCP discovery

| Tool | Key parameters | Description |
|---|---|---|
| `portunus_discover` | `project: str`, `register?: bool` | List what exists in a live GCP Secret Manager project (names + labels, never values). With `register=True`, writes not-yet-registered entries as `state=requested` placeholders and warms the local cache for `sync_mode=cached` projects. |

### Sync

| Tool | Key parameters | Description |
|---|---|---|
| `portunus_sync` | `project: str` | Force a recency check (and re-fetch if stale) for every `sync_mode=cached` reference in a project. Returns `{"synced", "already_fresh", "failed"}`. |

### Leak scan

| Tool | Description |
|---|---|
| `portunus_leak_status` | Read-only: severity, finding counts, and timestamps for one reference (`name` set) or all with active findings. Pass `detail=True` for per-finding path/line info. |
| `portunus_run_leak_scan` | Run a scan over the currently configured paths. Returns `{"configured": bool, "findings": [{ref_name, path, line_number}]}` — never a value, never file content. |
| `portunus_leak_scan_config_show` | Show the currently configured scan-path globs. |
| `portunus_leak_scan_config_add_path` | Add a glob to the scan config (e.g. `"~/.claude/projects/**/*.jsonl"`). Idempotent. |
| `portunus_leak_scan_config_remove_path` | Remove a glob from the scan config. Idempotent. |
| `portunus_leak_mark_rotated` | Assert that a reference has been rotated at its provider — clears active findings and resets the escalation clock. |

## Typical agent workflows

**Find and inject a secret:**
```python
# 1. Find the right reference name
refs = portunus_list(project="dostal.com")          # or portunus_search("stripe", env="prod")

# 2. Resolve to a temp file to hand to a subprocess
result = portunus_resolve_to_tempfile(name="stripe-live")
# result = {"path": "/tmp/portunus-XXXX"}
# Pass result["path"] to the command; do NOT read the file yourself.

# Or resolve-exec directly:
result = portunus_resolve_exec(
    argv=["curl", "-H", "Authorization: Bearer {{secret}}", "https://api.stripe.com/v1/charges"],
    name="stripe-live"
)
```

**Store a new secret handed to you by the user:**
```python
# Drop it (value flows in once; never echo it back)
portunus_drop(name="new-key", sm_name="dostal-new-key", value=the_value, project="dostal.com", env="prod")
# Make it injectable
portunus_state(name="new-key", state="enabled")
```

**Check what's available before starting a task:**
```python
tree = portunus_tree(project="ffe-cicd")
# Navigate tree["tree"] and tree["refs"] for group hierarchy and related links.
```
