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

mcp = FastMCP("portunus")


@mcp.tool()
def portunus_health() -> str:
    """Liveness check for the Portunus MCP server itself. Deliberately
    trivial -- mirrors ui/app/api/health/route.ts: never touches the
    registry, a backend, or PORTUNUS_HOME. Answers "is this process alive",
    not "is the vault healthy"."""
    return "ok"


def run_server() -> None:
    """Entry point for `portunus mcp` -- starts the stdio MCP server."""
    mcp.run()
