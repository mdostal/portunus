# Design Discussion: portunus-mcp-server

## 1. What Are We Doing?

Making Portunus directly usable by other agents/harnesses, not just this one Claude Code
session's CLI/UI: a new `portunus mcp` subcommand starts a local MCP (Model Context Protocol)
stdio server exposing Portunus's metadata queries and boundary-safe injection as MCP tools, so
an agent can say "call the Gemini API with my personal key" and get back a real response —
never the key itself. Plus a bounded `portunus auth login`/`status` surface so GCP account
lifecycle goes through Portunus as the single control surface instead of bare `gcloud`.

"Done" means: another agent (a different Claude Code session, a subagent, any MCP-capable
harness) can attach to `portunus mcp`, call tools to browse/find the right secret, and either
get a safe tempfile pointer or have Portunus make an authenticated call on its behalf — proven
for real against the personal Gemini key, not a mock.

## 2. What I Found

See `docs/research-brief.md`. The two load-bearing findings: MCP is already a working, proven
pattern in this exact environment (a local `FastMCP` stdio server via `uv run --script`,
registered in `~/.claude.json`), and `Resolver.resolve_exec()` already accepts a pluggable
`runner` — the "run a command with a secret injected, capture the output" primitive already
exists and is already tested, just never threaded through a CLI/server surface that actually
returns the captured output instead of process-replacing.

## 3. My Proposed Approach

**Slice A — `portunus mcp` scaffold.** Add `mcp>=1.2.0` to `pyproject.toml` dependencies. New
`portunus mcp` CLI subcommand starts a `FastMCP("portunus")` stdio server (`mcp.run()`). One
trivial tool first (`portunus_health` — process-alive check, no registry access) to prove the
wiring end-to-end before any real tool logic.

**Slice B — Read-only metadata tools.** `portunus_list(project)`, `portunus_tree(project=None)`,
`portunus_ask_preview(request)` (resolve-only — reuses `parse_intent`/`resolve_by_tags`, never
routes to add/rotate/list intent kinds, never injects), `portunus_bindings_show(project=None)`.
Each calls the library directly (`Registry`, `list_by_project`, the tree-building helpers,
`load_gcp_bindings`) — safe by the same structural guarantee already proven for these
functions (no path to `SecretBackend.access()`). Descriptions on each `@mcp.tool()` follow the
existing `.claude/skills/portunus-ask/SKILL.md` contract-explaining tone, since an MCP tool's
docstring is the only guidance the calling agent gets.

**Slice C — Discovery tool.** `portunus_discover(project, register=False)` — wraps
`list_gcp_secrets()`/`register_discovered()`/`diff_against_registry()` directly (still
metadata-only, same structural guarantee as the CLI/UI versions).

**Slice D — `resolve_to_tempfile` injection tool.** `portunus_resolve_to_tempfile(name="",
tags=None)` *(Grill H1 — resolved: mirrors the CLI's own dual-addressing convention, not a
vague combined param)* — `registry.require(name)` if `name` is given, else
`registry.resolve_by_tags(**tags)` if `tags` is given (fail-closed `NoMatch`/`AmbiguousMatch`
surfaced as a clear tool error). The tool builds the `{{secret:<resolved.name>}}` placeholder
itself and calls `Resolver.resolve_to_tempfile()`, returning the path only — the agent never
writes placeholder syntax itself, it just names what it wants (from a prior `list`/`tree`/
`ask_preview` call, or tags it already knows). The calling agent is responsible for not echoing
the file's contents back into its own response (documented explicitly in the tool description,
mirroring the existing skill's "you never see the value" framing) — same trust boundary as the
CLI's `resolve` command handed to any script/agent today.

**Slice E — `resolve_exec` injection tool (the "make the call for me" tool).** `portunus_
resolve_exec(argv, name="", tags=None)` — same addressing resolution as Slice D, then calls
`Resolver.resolve_exec(resolved_argv, runner=capturing_runner)` where `capturing_runner` is
`subprocess.run(resolved_argv, capture_output=True, text=True, timeout=30)`. Returns **only**
`{stdout, stderr, returncode}` from that subprocess — never the resolved argv (which contains
the secret value substituted into one element). This is the tool that directly satisfies "give
them the personal gemini key" — an agent asks Portunus to run `curl ... -H "x-goog-api-key:
{{secret:...}}" ...` and gets back the API response, never the key.

**Slice F — Auth lifecycle through Portunus.** `portunus auth login <email>` (thin
`subprocess.run(["gcloud", "auth", "login", email])` wrapper — still opens a real browser,
Portunus doesn't remove that requirement, it's the single command a human/agent remembers).
`portunus auth status` — cross-references `gcloud auth list`'s credentialed accounts against
every project's configured `bindings.account`, reporting which bindings' accounts are
currently authenticated vs. which would fail. Explicitly NOT automatic reauth (research-brief
scope decision) — a clear status report plus a wrapped login command, not a background daemon.

**Slice G — Closeout.** Register `portunus mcp` in this Claude Code environment's
`~/.claude.json` (`claude mcp add`), verify via a raw stdio JSON-RPC test script (list_tools +
call each tool) since this session can't attach to a server started mid-conversation.
Real proof: promote `personalsites-487021-google_generative_ai_api_key` to `enabled` (human
decision, made live), call `portunus_resolve_exec` with a real `curl` against the Gemini API,
confirm a real API response comes back and the key never appears anywhere in the tool's
response. README/CONTEXT.md, extend/cross-reference the existing Claude skill, version bump,
changelog.

## 4. What Could Go Wrong

- **[critical] `resolve_exec`'s return value could leak the secret if the wrapped subprocess
  echoes it (e.g. `echo {{secret:x}}` instead of using it as a header) or if an exception path
  includes the resolved argv in its message.** Mitigation: the tool's own code only ever
  touches `{stdout, stderr, returncode}` from the `subprocess.run()` result object — never
  `resolved_argv` — at every return statement including exception handlers; documented
  explicitly in the tool description that the wrapped command is the caller's responsibility
  not to echo the secret (same boundary contract the CLI's `resolve --exec` already has today
  for any script/agent using it) — Portunus's own code cannot prevent a deliberately
  malicious/careless caller-supplied command from printing what it was given, same as today.
- **[high] An MCP tool exception message could accidentally stringify something containing a
  value** (e.g. a raw `subprocess.CalledProcessError` repr in some Python versions can include
  full argv). Mitigation: every tool wraps its call in an explicit try/except that constructs
  its OWN error message from known-safe fields (reference name, error type) — never
  `str(exception)` on anything that touched a resolved value.
- **[medium] `portunus_ask_preview` could accidentally route through an intent_kind other than
  `fetch`** (e.g. misclassify as `list`/`add`/`rotate`), producing different behavior than
  intended for a "preview" tool. Mitigation: the tool explicitly checks `intent_kind ==
  "fetch"` and returns a clear "not a fetch request" message for any other kind, rather than
  silently executing whatever `ask`'s CLI would do for that kind.
- **[low] Auth status reporting could go stale immediately** (a token can expire seconds after
  `portunus auth status` reports it healthy). Out of scope to solve — status is a point-in-time
  check, same limitation `gcloud auth list` itself has.

## 5. Dependencies and Constraints

- Slice A is a hard prerequisite for everything else.
- Slices B/C/D can build in parallel once A lands.
- Slice E depends on A only (independent of B/C/D, but reviewed last given its risk level).
- Slice F is fully independent of B–E.
- Slice G depends on all of B–F (closeout needs every tool to exist).
- `secret-boundary-invariant` is the dominant constraint this entire epic, more explicitly than
  any prior one — see §4.

## 6. Open Questions

None outstanding — MCP registration mechanics, the runner-injection primitive, and the
boundary-safety pattern for each tool were all resolved directly against real, already-proven
code/environment during research; no design fork remained.

## 7. Verification Strategy

```
VERIFICATION PLAN:
  Tools: pytest (library-level tool logic, mocked where a real subprocess isn't needed), a raw
    stdio JSON-RPC test script for the actual MCP protocol surface, real live API call for the
    closeout.
  Automated: every read-only tool's structural no-backend-access guarantee (mirrors existing
    discover.py/list_by_project tests); resolve_to_tempfile tool never returns a value;
    resolve_exec tool's return object contains only stdout/stderr/returncode keys, verified by
    key-set assertion, plus an explicit test that a secret value injected into the mocked
    subprocess's stdout does NOT propagate if the tool's own code path doesn't include it
    (i.e. testing the code never touches resolved_argv, not just that this one test's fake
    value happens not to appear).
  Manual: `claude mcp add` this repo's `portunus mcp`; a raw JSON-RPC script exercising
    tools/list and tools/call for each tool against a temp PORTUNUS_HOME; the real Gemini-key
    resolve_exec proof against the live Google Generative AI API.
  Not verifying: fully automatic GCP reauth (explicitly out of scope, §research-brief).
```

## 8. Scale Assessment

```
SCALE ASSESSMENT:
  Files affected: ~6 (pyproject.toml, cli.py [mcp subcommand + auth login/status], new
    src/portunus/mcp_server.py, README/CONTEXT.md, tests)
  Subsystems: new MCP server module, CLI (auth lifecycle), OSTIARIUS (a third entry point
    alongside cli.py and the UI's API routes)
  Migration required: no
  Cross-team coordination: no
  Unknowns: 0

  RECOMMENDATION: Proceed to stories (skip H/V) -- MCP registration mechanics are already
    proven in this environment (not a novel unknown), the injection primitives already exist
    and are tested, and the one genuinely new risk (resolve_exec's boundary safety) is a
    single, well-scoped story with explicit, itemized mitigations.
  RATIONALE: Medium scope by file count, but the core technical risk is concentrated and
    already well-understood (§4) rather than diffuse -- appropriate for direct-to-stories.
```
