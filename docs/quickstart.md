# Quickstart

Zero to your first `resolve --exec` in a few minutes.

---

## 1. Install

One command installs the CLI, wires the MCP server into any AI agent CLIs already on your machine, and drops in usage skills:

```bash
curl -fsSL https://mdostal.github.io/portunus/install.sh | bash
```

Or, if you prefer pip:

```bash
pip install git+https://github.com/mdostal/portunus.git
```

Or from a clone (includes the test suite):

```bash
git clone https://github.com/mdostal/portunus
cd portunus
pip install -e ".[test]"
```

Requires Python ≥ 3.9. The default vault backend is local-encrypted — no cloud account needed to start.

---

## 2. Store your first secret

Use `--stdin` — never an inline flag. Anything passed directly on the command line lands in your shell history and `ps` output; stdin never does.

```bash
echo -n "sk-ant-api-..." | portunus drop my-anthropic dostal-shared-anthropic --stdin
```

- `my-anthropic` — the local reference name you'll use to look it up later.
- `dostal-shared-anthropic` — the Secret Manager name (can be anything descriptive; matches the key in your vault backend).

The secret is now stored in your local encrypted vault and is in `state=dropped` — it is **not yet injectable**. That's intentional; see [Concepts → Fail-closed default](concepts.md#fail-closed-default).

---

## 3. Enable the reference

A freshly dropped reference starts fail-closed. Explicitly enable it when you've confirmed it's correct:

```bash
portunus state my-anthropic enabled
```

Now it's injectable. You can also lock a reference (`state locked`) to freeze further edits while keeping it injectable, or revoke it (`state revoked`) to block all injection immediately.

---

## 4. Resolve at the boundary

The entire point of Portunus: the secret value never touches your shell history, a log line, or an LLM context. It's injected *only* at the execution boundary — the actual outbound call.

```bash
portunus resolve --exec curl \
  -H "Authorization: Bearer {{secret:my-anthropic}}" \
  https://api.anthropic.com/v1/messages \
  -d '{"model":"claude-sonnet-5","max_tokens":10,"messages":[{"role":"user","content":"hi"}]}'
```

Portunus substitutes `{{secret:my-anthropic}}` with the real value in the child process's argv only. It never appears in this shell, in any log, or in the curl command as seen by `ps`.

---

## 5. Inspect what you have

```bash
portunus list                          # every reference (metadata only, never values)
portunus list --project my-project     # filter by project tag
portunus show my-anthropic             # one reference's full metadata
```

`list` and `show` return reference metadata — name, SM name, state, tags, lifecycle status. They never return a value. Neither does `portunus tree` (hierarchy view) or any MCP tool by default.

---

## 6. Wire the MCP server into your AI agent

If you installed with the one-liner above, this is already done. To check, or to re-run after installing a new harness:

```bash
portunus agent init      # detects Claude Code / Codex CLI and wires each one found
portunus agent status    # shows what's currently registered, without changing anything
```

`agent init` is idempotent — safe to run any time. It registers the MCP server (`portunus mcp`) and installs the usage skills to `~/.claude/skills/` so Claude Code sessions on this machine get boundary-only conventions automatically, not just a raw tool list.

With the MCP server wired, an agent session can call tools like `portunus_list`, `portunus_resolve_exec`, and `portunus_drop` without shelling out to the CLI — the same registry, the same gate, the same audit trail.

---

## Next steps

- **[Core concepts](concepts.md)** — what Reference, ARCA, OSTIARIUS, Petitio, and the audit chain each do and why they exist.
- **[Architecture](architecture.md)** — component diagrams, ARCA backend-selection precedence, the full request/resolve sequence, and access control (Petitio/roles).
- **[README](https://github.com/mdostal/portunus/blob/main/README.md)** — the full CLI reference: tag-based lookup, GCP multi-project setup, vault backup, leak detection, and more.
