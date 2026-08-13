"""OSTIARIUS — the MCP entry point.

A third gatekeeper surface alongside cli.py (the terminal) and the UI's API
routes: an MCP (Model Context Protocol) stdio server so other agents and
harnesses -- not just this one Claude Code session -- can query and inject
Portunus secrets directly. Same rule as everywhere else in this codebase: a
tool's return value must never contain a resolved secret value, on any path
including exceptions. Unlike the UI (a different language runtime shelling
out to the CLI), this module calls the Portunus library directly -- same
package, same process, no subprocess boundary needed for that reason alone.
"""
from __future__ import annotations

import subprocess
from typing import List, Optional

from mcp.server.fastmcp import FastMCP

from .backend import BackendError, load_gcp_bindings
from .broker import ApprovalRequired, NotInjectable
from .cli import _build, _build_tree, _wif_configured
from .discover import DiscoverError, diff_against_registry, list_gcp_secrets, register_discovered
from .intent import AmbiguousIntent, classify_intent_kind, parse_intent
from .registry import AmbiguousMatch, NoMatch, Registry
from .resolver import UnknownReference

mcp = FastMCP("portunus")


class AddressError(Exception):
    """Raised when a tool's name/tags addressing can't identify a reference
    before resolution -- distinct from NoMatch/AmbiguousMatch, which mean
    "addressing was well-formed but resolution failed/was ambiguous"."""


def _resolve_address(registry: Registry, name: str, tags: Optional[dict]):
    """Dual addressing (Grill H1, portunus-mcp-server): resolve by exact
    name if given, else by tags. Mirrors the CLI's own inject/ask dual-
    addressing convention -- the caller never writes {{secret:NAME}}
    placeholder syntax, it just names what it wants."""
    if name:
        try:
            return registry.require(name)
        except KeyError:
            raise AddressError(f"unknown reference: {name}")
    if tags:
        return registry.resolve_by_tags(**tags)  # NoMatch/AmbiguousMatch propagate
    raise AddressError("name or tags is required")


@mcp.tool()
def portunus_health() -> str:
    """Liveness check for the Portunus MCP server itself. Deliberately
    trivial -- mirrors ui/app/api/health/route.ts: never touches the
    registry, a backend, or PORTUNUS_HOME. Answers "is this process alive",
    not "is the vault healthy"."""
    return "ok"


@mcp.tool()
def portunus_list(project: str) -> list:
    """List every secret registered for `project` -- metadata only
    (name, description, purpose, state, tags, etc), never a value.
    Use this to find the exact reference name you need before calling
    portunus_resolve_to_tempfile or portunus_resolve_exec."""
    registry = Registry()
    return [ref.to_dict() for ref in registry.list_by_project(project)]


@mcp.tool()
def portunus_tree(project: str = "") -> dict:
    """Render secrets by group hierarchy + related links -- metadata only,
    never a value. Same shape as `portunus tree --json`: {ungrouped, tree,
    refs}. A reference with no group appears in `ungrouped`, never dropped."""
    registry = Registry()
    refs = list(registry)
    if project:
        refs = [r for r in refs if r.project == project]
    if not refs:
        return {"ungrouped": [], "tree": {}, "refs": {}}
    ungrouped, tree, refs_meta = _build_tree(refs)
    return {"ungrouped": sorted(ungrouped), "tree": tree, "refs": refs_meta}


@mcp.tool()
def portunus_ask_preview(request: str) -> dict:
    """Preview what secret a plain-language request would resolve to --
    metadata only, never a value, never injects anything. Only handles
    fetch requests (e.g. "the vercel secret for mdostal.com in prod"); a
    request that reads as an add/rotate/list request is rejected with a
    clear message rather than silently executing that other behavior.
    Fails closed on anything ambiguous or unrecognized -- never guesses."""
    registry = Registry()
    intent_kind = classify_intent_kind(request.lower())
    if intent_kind != "fetch":
        return {
            "error": f"not a fetch request (classified as {intent_kind!r}) -- "
            "portunus_ask_preview only previews fetch requests"
        }
    try:
        tag_set = parse_intent(request, registry)
    except AmbiguousIntent as exc:
        return {"error": exc.clarifying_question}
    try:
        ref = registry.resolve_by_tags(**tag_set)
    except NoMatch:
        return {"error": f"no reference matches the inferred tags: {dict(tag_set)!r}"}
    except AmbiguousMatch as exc:
        return {"error": f"ambiguous match: {exc.candidates}"}
    return ref.to_dict()


@mcp.tool()
def portunus_bindings_show(project: str = "") -> dict:
    """Show configured GCP project bindings (account/WIF audience) -- one
    project or all. Same values as `portunus bindings show --json`."""
    bindings = load_gcp_bindings()
    if project:
        binding = bindings.get(project)
        if binding is None:
            return {}
        bindings = {project: binding}
    return {p: {"account": b.account, "wif_audience": b.wif_audience} for p, b in bindings.items()}


@mcp.tool()
def portunus_discover(project: str, register: bool = False) -> dict:
    """Read-only: list what already exists in a live GCP Secret Manager
    project (names + labels + create-time only, never a value). With
    register=True, writes not-yet-registered secrets as state=requested
    placeholders (never overwrites an existing reference -- a naming
    collision is reported, not silently replaced). Mirrors `portunus
    discover [--register] --json` exactly -- one safety-reviewed
    implementation, three entry points (CLI, UI, MCP)."""
    registry = Registry()
    try:
        discovered = list_gcp_secrets(project)
    except DiscoverError as exc:
        return {"error": str(exc)}

    if register:
        report = register_discovered(registry, project, discovered)
        return {
            "registered": report.registered,
            "conflicts": report.conflicts,
            "already_registered": report.already_registered,
            "wif_configured": _wif_configured(project),
        }

    already, not_yet = diff_against_registry(registry, project, discovered)
    return {
        "already_registered": already,
        "not_yet_registered": [
            {"sm_name": d.sm_name, "labels": d.labels, "create_time": d.create_time}
            for d in not_yet
        ],
        "wif_configured": _wif_configured(project),
    }


@mcp.tool()
def portunus_resolve_to_tempfile(name: str = "", tags: Optional[dict] = None) -> dict:
    """Resolve one secret to a 0600 temp file and return its PATH -- never
    the value. Address it by exact reference `name` (from a prior
    portunus_list/portunus_tree/portunus_ask_preview call) OR by `tags`
    (e.g. {"provider": "gcp", "project": "demo"}) -- give one, not both.
    Fails closed on anything ambiguous, unknown, or not currently
    injectable (dropped/revoked/requested/gated). You are responsible for
    not reading the file's contents back into your own response -- treat
    the path as a pointer to hand to another tool/command, not something
    to inspect yourself."""
    registry, _audit, broker, resolver = _build()
    try:
        ref = _resolve_address(registry, name, tags)
    except AddressError as exc:
        return {"error": str(exc)}
    except NoMatch:
        return {"error": f"no reference matches tags: {tags!r}"}
    except AmbiguousMatch as exc:
        return {"error": f"ambiguous match: {exc.candidates}"}

    placeholder = f"{{{{secret:{ref.name}}}}}"
    try:
        path = resolver.resolve_to_tempfile(placeholder)
    except UnknownReference:
        return {"error": f"unknown reference: {ref.name}"}
    except (NotInjectable, ApprovalRequired) as exc:
        return {"error": str(exc)}
    except BackendError as exc:
        return {"error": str(exc)}
    return {"path": path}


class _ExecTimeout(Exception):
    """Sentinel: the subprocess timed out. Deliberately carries no cmd/argv
    attribute -- str(subprocess.TimeoutExpired) embeds the resolved command
    line, which may contain a substituted secret value."""


class _ExecFailed(Exception):
    """Sentinel: subprocess.run() raised something other than a timeout
    (e.g. an invalid executable). Deliberately carries no argv attribute --
    OSError's own message embeds the argv it was given."""


def _capturing_runner(resolved_argv):
    """The runner Resolver.resolve_exec() calls with the fully-substituted
    argv -- this is the ONLY place in the process that ever holds a resolved
    secret value for this tool. Never re-raises the underlying exception:
    both subprocess.TimeoutExpired and OSError carry the resolved argv on
    their own attributes/message, so a bare sentinel is raised instead."""
    try:
        return subprocess.run(resolved_argv, capture_output=True, text=True, timeout=30)
    except subprocess.TimeoutExpired:
        raise _ExecTimeout() from None
    except OSError:
        raise _ExecFailed() from None


@mcp.tool()
def portunus_resolve_exec(argv: List[str], name: str = "", tags: Optional[dict] = None) -> dict:
    """Run a command with one secret substituted in, and return only the
    command's result -- never the secret or the resolved command line.
    Address the secret by exact reference `name` (from a prior
    portunus_list/portunus_tree/portunus_ask_preview call) OR by `tags` --
    give one, not both (same dual addressing as portunus_resolve_to_tempfile).
    In `argv`, write the literal marker "{{secret}}" wherever the value
    should be substituted, e.g.
    ["curl", "-H", "x-goog-api-key: {{secret}}", "https://..."] -- you do
    not need to know or write the reference's own {{secret:NAME}} syntax,
    Portunus builds that from the resolved reference.

    Returns {"stdout": ..., "stderr": ..., "returncode": ...} from the
    subprocess (30s timeout). A non-zero returncode is NOT treated as a
    tool-level error -- it is returned normally for you to interpret; that
    is the wrapped command's own success/failure semantics, not Portunus's.
    Fails closed (an "error" key only) on unknown/ambiguous/non-injectable
    addressing or a timeout/exec failure -- never a resolved value.

    Portunus cannot control what the command you supply chooses to print:
    if your own command echoes its arguments back (e.g. `echo {{secret}}`),
    the secret will appear in its stdout, and that stdout IS returned to
    you as-is. This tool's guarantee is only that Portunus's own return path
    never independently contains the resolved command line or a value
    Portunus itself extracted -- it is not a guarantee about a command that
    deliberately echoes its own input."""
    registry, _audit, broker, resolver = _build()
    try:
        ref = _resolve_address(registry, name, tags)
    except AddressError as exc:
        return {"error": str(exc)}
    except NoMatch:
        return {"error": f"no reference matches tags: {tags!r}"}
    except AmbiguousMatch as exc:
        return {"error": f"ambiguous match: {exc.candidates}"}

    placeholder = f"{{{{secret:{ref.name}}}}}"
    template_argv = [arg.replace("{{secret}}", placeholder) for arg in argv]

    try:
        result = resolver.resolve_exec(template_argv, runner=_capturing_runner)
    except UnknownReference:
        return {"error": f"unknown reference: {ref.name}"}
    except (NotInjectable, ApprovalRequired) as exc:
        return {"error": str(exc)}
    except BackendError as exc:
        return {"error": str(exc)}
    except _ExecTimeout:
        return {"error": "command timed out after 30s"}
    except _ExecFailed:
        return {"error": "command failed to start"}
    return {"stdout": result.stdout, "stderr": result.stderr, "returncode": result.returncode}


def run_server() -> None:
    """Entry point for `portunus mcp` -- starts the stdio MCP server."""
    mcp.run()
