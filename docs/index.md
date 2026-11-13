# Portunus

> A standalone, boundary-only secret broker — reference a secret by name; the value is injected only at the execution boundary, never inside an LLM or agent context.

## Documentation

- **[Quickstart](quickstart.md)** — Install, store your first secret, and run your first `resolve --exec` in a few minutes.
- **[Core Concepts](concepts.md)** — Reference, ARCA, OSTIARIUS, Petitio, injection modes, the audit chain, and the fail-closed default.
- **[Architecture](architecture.md)** — Component diagrams, ARCA backend-selection precedence, the full request/resolve sequence, and opt-in access control.
- **[Rotation Guide](rotation.md)** — What rotates automatically, what doesn't, and what you need to do by hand.
- **[Provider Rotation Matrix](provider-rotation-matrix.md)** — Per-provider rotation capability reference.
- **[MCP Server & Agent Integration](mcp-server.md)** — Wire Portunus into Claude Code and Codex CLI, and the tools agents get.
- **[Portunus with T3 Code](t3-code.md)** — Store what you type into a T3 Code secret card in the vault, and answer cards and settings fields from the vault.

## Links

- **Source:** [github.com/mdostal/portunus](https://github.com/mdostal/portunus)
- **Install:** `curl -fsSL https://mdostal.github.io/portunus/install.sh | bash`
