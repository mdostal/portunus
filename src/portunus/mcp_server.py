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

from mcp.server.fastmcp import FastMCP

from .backend import load_gcp_bindings
from .cli import _build_tree, _wif_configured
from .discover import DiscoverError, diff_against_registry, list_gcp_secrets, register_discovered
from .intent import AmbiguousIntent, classify_intent_kind, parse_intent
from .registry import AmbiguousMatch, NoMatch, Registry

mcp = FastMCP("portunus")


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


def run_server() -> None:
    """Entry point for `portunus mcp` -- starts the stdio MCP server."""
    mcp.run()
