---
name: portunus-vault-audit
description: Bundle missing-metadata context for the vault (crawl), render current vault state as a deploy-docs report, or run a leak-scan over the human-configured local paths and check/clear findings. Use when the user asks you to "check the vault," "see what's missing," "generate deploy docs," "scan for leaked secrets," or "check if anything leaked" -- never to fetch or handle a secret's actual value, which this skill never touches.
---

# Portunus Vault Audit

Thin wrapper around three read-mostly Portunus operations that all answer "what's the state of
the vault, and does anything need attention" rather than resolving/injecting a value: `crawl`
(metadata gaps), `report` (a Markdown deploy-docs snapshot), and `leak-scan` (has a managed
secret's value shown up somewhere it shouldn't). None of these ever return, print, or need you
to handle a secret value.

## When to use

- "What's missing from the vault's metadata?" / "help me fill in descriptions" -> crawl.
- "Generate deploy docs for this vault" / "what secrets does this project use and why" ->
  report.
- "Check if any of our secrets leaked into logs/chat history/.claude" / "scan for leaks" ->
  leak-scan.
- "Is `<reference>` still flagged as leaked?" / "I rotated it, clear the warning" -> leak
  status / mark-rotated.

## When NOT to use

- You need to fetch or inject a secret's actual value — use `portunus-ask` instead. This skill
  never resolves a value.
- You need to store a brand-new secret — use `portunus-drop`.
- You need to configure which backend a project uses — use `portunus-vault-setup`.

## Crawl — metadata-gap discovery, never automatic

```bash
portunus crawl --json                 # every reference missing description/purpose/org
portunus crawl --org <org> --json     # scoped
```

This bundles context (sm_name, group, project, org, repo, its vault/rotation binding) for YOU
to read and propose values from — it never calls an LLM itself and never writes anything. If
you have a real proposal for a field, call `portunus metadata confirm` (or the
`portunus_suggest_metadata` MCP tool) — never edit `registry.json` directly.

## Report — a real deploy-docs starting point

```bash
portunus report                        # prints Markdown to stdout
portunus report --out deploy-docs.md    # writes to a file
portunus report --org <org>             # scoped
```

Independent of crawl — useful immediately, with or without any crawl-sourced metadata.

## Leak-scan — advisory only, never blocks anything

**Scan paths are never auto-configured.** Before a scan finds anything, paths must be
explicitly added:

```bash
portunus leak-scan config show                      # what's currently configured (often empty)
portunus leak-scan config add-path '<glob>'          # e.g. ~/.claude/projects/**/*.jsonl
portunus leak-scan config remove-path '<glob>'
```

Then:

```bash
portunus leak-scan --json      # run a scan; exits 1 if new findings exist, 0 otherwise
portunus leak status           # severity + finding counts for every reference with findings
portunus leak status <name>    # for one reference
portunus leak mark-rotated <name>   # after YOU (or the user) have actually rotated it
```

**This is a detective control, not a preventive one.** It finds secrets that already leaked
into the paths you've configured; it does nothing to stop the next paste into a chat window,
and it never blocks `resolve`/`inject`. The output ever contains only `{ref_name, path,
line_number, severity}` — never the leaked value itself and never a surrounding excerpt.
`mark-rotated` is your own assertion that you rotated the credential — Portunus cannot verify
that independently, and a later scan will still re-flag the reference if the old value is
genuinely still present somewhere.

**MCP equivalents** (for a harness that isn't this CLI session, or fully automated pipelines):
`portunus_crawl_candidates`, `portunus_leak_status`, `portunus_run_leak_scan`,
`portunus_leak_scan_config_show/add_path/remove_path`, `portunus_leak_mark_rotated`. Same
gating, same never-a-value guarantee as the CLI above.

## Scheduling a scan (cron/CI)

`portunus leak-scan` is already cron/CI-ready: it exits `1` when new findings exist and `0`
otherwise, and never prompts. A simple periodic check (see README.md's "Scheduling leak-scan"
section for a full cron/launchd example):

```bash
0 9 * * * PORTUNUS_HOME=/path/to/home portunus leak-scan --json >> /path/to/leak-scan.log 2>&1
```

Do not wire this to auto-rotate or auto-block anything — v1 is advisory-only by design
(`docs/architecture.md` §11).
