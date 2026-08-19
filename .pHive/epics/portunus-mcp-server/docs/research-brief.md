# Research Brief: portunus-mcp-server

## Requirement

User's own words: "portunus needs to have the re-auth, refresh, etc in there, let you fully
config and maintain it through portunus, and then we need full skills, mcp, etc plugins so
that portunus as a local service stood up can be fully utilized by other agents and harnesses
to do the injection expected. and i'd love to start there so they stop asking for keys and it
can just give them the personal gemini key etc."

Two pieces:
1. Auth lifecycle managed through `portunus` itself (not raw `gcloud` out-of-band).
2. An MCP server (+ skill) so other agents/harnesses can query and inject secrets through
   Portunus directly — explicitly prioritized ("i'd love to start there"), motivated by a
   concrete example: another agent needing the personal Gemini key should get it injected via
   Portunus, never by asking the user to paste it.

## Real infra confirmed this session (do not re-verify, use as given)

- MCP is already a live, working pattern in this exact environment: `claude mcp list` shows
  several configured servers, including a local stdio one
  (`~/.claude/mcp/qdrant-nomic-mcp.py`) registered as
  `{"type": "stdio", "command": "uv", "args": ["run", "--script", "<path>"], "env": {}}` in
  `~/.claude.json`'s `mcpServers` map. That script uses the official Python MCP SDK's
  `mcp.server.fastmcp.FastMCP` class, PEP 723 inline dependencies (`# /// script` block), and
  `uv run --script` for zero-install execution -- `@mcp.tool()`-decorated functions become MCP
  tools, `mcp.run()` starts the stdio server.
- `uv` is installed (`0.6.14`) and already the working mechanism for local Python MCP servers
  in this Claude Code environment.
- The real Gemini secret to use as the closeout proof:
  `demo-project-483920-google_generative_ai_api_key` (sm_name `GOOGLE_GENERATIVE_AI_API_KEY`),
  currently `state=requested` from the earlier bulk-discovery -- needs promotion to `enabled`
  (a human decision, made live in this conversation, same pattern as the Resend key earlier)
  before it's usable.

## Current state (verified against code)

- `Resolver.resolve_exec(argv, runner=None)` (resolver.py) already supports a pluggable
  `runner` callable instead of the default `os.execvp` process-replace -- tests already use
  this (`runner=fake_runner`) to capture resolved argv without ever calling a shell. The CLI's
  `cmd_resolve` never threads this through (`resolve --exec` always process-replaces) --
  there is no existing "run a command with a secret injected and capture its output" surface
  today, but the library primitive for it already exists and is already tested.
- `Resolver.resolve_to_tempfile(text)` already exists and is exactly the "give me a safe
  pointer to the value" primitive -- writes a 0600 file, returns the path, never the value.
- `Registry.list_by_project()`, the CLI's `_build_tree()`/`_render_tree_text()` (currently
  private functions in `cli.py`), `discover.py`'s `list_gcp_secrets()`/`register_discovered()`,
  and `load_gcp_bindings()` are all already-built, already-safety-reviewed metadata-only
  primitives an MCP server can call directly as a library, in-process -- no subprocess
  shell-out needed (unlike the UI, which is a different language runtime; the MCP server is
  Python, same package, same process space).
- `pyproject.toml`'s `[project.scripts]` defines exactly one console script,
  `portunus = "portunus.cli:main"`. Adding `mcp` as a new subcommand of the existing CLI
  (`portunus mcp`) means zero new binaries to install/discover -- `claude mcp add` can point
  directly at the already-on-PATH `portunus` command.
- No existing CLI command wraps `gcloud auth login`/`gcloud auth list` -- a human always runs
  bare `gcloud` for this today (as the user did twice this session). `portunus bindings`
  already exists (project -> account/audience config) but nothing surfaces account *health*
  (which local accounts exist, which is active, whether each configured binding's account is
  actually currently authenticated).
- `.claude/skills/portunus-ask/SKILL.md` already exists and documents `portunus ask`'s
  boundary-safe contract for agent use -- a precedent for how to *document* a boundary-safe
  tool to an agent (never read/echo the value), directly reusable as the tone/contract model
  for the new MCP tool descriptions.

## Constraints (cross-cutting-concerns.yaml)

`secret-boundary-invariant` is the central design driver of this entire epic, more than any
prior one: an MCP tool's return value flows **directly into the calling agent's own LLM
context** -- there is no terminal/human in between to notice a leaked value the way there
might be with a CLI. Every tool must be reviewed against: does its return value, on the
success path AND every failure/exception path, ever contain a resolved secret value? Read-only
tools (list/tree/discover/bindings-show) are safe by construction (they never call
`SecretBackend.access()`, same structural guarantee already proven for `list_by_project`/
`discover.py`). The two injection tools (`resolve_to_tempfile`, `resolve_exec`) need explicit,
individual scrutiny: `resolve_to_tempfile` already has this guarantee (returns a path, not a
value, by the existing library contract). `resolve_exec` is new territory -- it must return
the subprocess's own stdout/stderr/exit code, and must NEVER return the resolved argv (which
contains the secret value substituted into one of its elements).

## Scope decision for this pass

- MCP server calls the Portunus library directly (in-process), not via subprocess shell-out to
  the CLI -- unlike the UI (a different language runtime), the MCP server is Python in the same
  package, so importing `Registry`/`Resolver`/`Broker` directly is the natural, already-used
  pattern (matches how `cli.py` itself is structured, not a new precedent).
- Auth lifecycle scope is bounded: `portunus auth login <email>` (wraps `gcloud auth login`)
  and `portunus auth status` (cross-references `gcloud auth list` against configured
  bindings). Explicitly NOT attempting fully-automatic reauth (that requires either a
  long-lived service-account key -- against `assert_no_long_lived_cloud_keys()` -- or a real
  WIF trust relationship, neither in scope here) -- the goal is "one control surface"
  (`portunus`, not bare `gcloud`), not "never touch a browser again."
- The existing `.claude/skills/portunus-ask/` skill is kept as-is (works even for harnesses
  that only shell out, no MCP attachment) -- this epic adds MCP as a new, richer capability
  alongside it, not a replacement.
