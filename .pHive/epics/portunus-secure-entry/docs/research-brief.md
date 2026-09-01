# Research brief: portunus-secure-entry

## 1. The user's own framing

> "we need a way for an agent using portunus to open a place to enter the key and have it get
> saved without being viewed -- this can be a portunus dashboard that the agent can open, a
> tool, a TUI that obfuscates and hides it and does trim on surrounding whitespace etc -- we
> just need a way to enter keys easily when fighting our agents"

Two distinct real problems bundled in one ask:

1. **The mechanical UX gap**: today's only way to hand Portunus a value is `portunus drop
   <name> <sm_name> --stdin` (reads one raw line via `sys.stdin.readline()`, only
   `.rstrip("\n")` applied — no masking, no whitespace trim beyond the trailing newline) or
   `--value-file` (reads a file, same `.rstrip("\n")` only). A leading/trailing space, tab, or
   `\r` (a classic artifact of copy-pasting a token from a web page or a Windows-authored file)
   silently corrupts the stored value with no error anywhere — it just fails to authenticate
   later, at the worst possible time, disconnected from the entry mistake.
2. **The "fighting our agents" problem**: an agent's own tool-execution channel (Claude Code's
   Bash tool, same shape in Codex/other harnesses) is fundamentally non-interactive — it runs a
   command to completion and captures stdout/stderr; it never has a live TTY a human can type
   into. So an agent literally *cannot* run an interactive masked prompt itself and hand it to a
   human — verified directly: `getpass.getpass()` against closed/empty stdin (exactly what an
agent's non-interactive tool call provides) raises `EOFError` immediately, not a hang. The
agent's only real options are: (a) tell
   the human, in chat, to run a specific command themselves in their own terminal, or (b) fire
   off a command that opens something OUTSIDE its own tool-execution channel (a browser tab, a
   GUI app) and returns immediately without waiting on human input.

## 2. What already exists — confirmed directly, not assumed

**A masked-entry web dashboard already exists.** `ui/app/components/AddSecretForm.tsx` has a
`type="password"` value field, submits to `ui/app/api/drop/route.ts`, which pipes the value to
`portunus drop --stdin` via stdin only (never an argv element, never logged, never echoed back
— its own header comment calls this "the deliberate human-plaintext-entry point, Grill U1
resolution"). This is real, already shipped, and already never round-trips through an agent or
an LLM context — the browser-to-localhost-API path is entirely separate from any agent's tool
calls. **Confirmed gap**: the value is sent as `String(body.value ?? "")` with no `.trim()` —
the same whitespace-corruption risk as the CLI path, just server-side instead of stdin-side.

**Agent-initiated "add" requests already exist and already land as a value-less
placeholder.** `portunus ask "add an API key for X"` → `classify_intent_kind` → `add` →
`Registry.request()` creates a `state=requested` reference with every metadata field the agent
already knows (org/provider/project/env/tags/description/purpose) but never a value. This is
the exact mechanism that should feed a "here's what's pending" surface — **confirmed gap**: it
doesn't yet. `AddSecretForm`'s `initial` prop today is only ever pre-filled from a *rotate*
draft (`page.tsts`'s `rotateDraft`), never from a `state=requested` reference. `DetailDrawer.tsx`
has an `onRotate` action button but no equivalent "fulfill this request" action — a human
looking at a `state=requested` reference in the Console/ProjectExplorer has no one-click path
into `AddSecretForm` with that reference's metadata already carried over; they'd have to
manually retype name/sm_name/org/provider/project/env, error-prone and exactly the kind of
friction re-typing the ask was supposed to save.

**No "open the dashboard" command exists.** `cli.py` has no `ui`/`open`/similar subcommand.
Nothing lets an agent's own tool call trigger "pop the vault UI open, pointed at this pending
request" for the human, non-interactively (fire-and-forget, no TTY needed). This is the literal
"a portunus dashboard that the agent can open" half of the ask, and it's the one piece with no
existing partial implementation to build on.

**No interactive masked CLI prompt exists.** `cmd_drop` requires either `--stdin` (a raw
`readline()` — echoes whatever the terminal echoes, no masking) or `--value-file`. There is no
`getpass`-based mode at all. This is the literal "a TUI that obfuscates and hides it" half of
the ask, for the case where a human is at a bare terminal with no browser/UI running (e.g. SSH'd
into a headless box, or simply prefers the CLI).

## 3. Why this isn't `resolve`/`inject`'s problem to solve

Every existing boundary-safety mechanism (`Resolver.resolve_call`, `resolve_exec`,
`resolve_to_tempfile`) governs the *output* side — getting a value OUT of Arca and into an
execution boundary without an LLM ever seeing it. This ask is the *input* side — getting a
value INTO Arca in the first place, also without an LLM ever seeing it. `cmd_drop` already
gets this right structurally (`del value` after `store()`, never touches the audit log, never
an argv flag) — the gap here is purely UX: masking, trimming, and discoverability/reachability
for a human working alongside an agent, not a new secret-boundary invariant to invent.

## 4. Scope boundary

In scope: whitespace trimming at the one real entry-point choke (`cmd_drop`'s stdin/file read,
which the web route already funnels through); an interactive masked+confirmed CLI prompt mode
for `cmd_drop`; wiring `state=requested` references into the existing `AddSecretForm`/
`DetailDrawer` UI as a one-click "fulfill" action; a new `portunus ui open [--fulfill NAME]`
CLI command that opens the existing web UI (via `webbrowser.open()`, cross-platform stdlib, no
per-OS special-casing) at a URL that deep-links straight into the fulfill flow.

Out of scope, explicitly deferred (design-discussion.md self-grill): launching the packaged
desktop app specifically (vs. the web UI) — the desktop app is "packaging, not a new component"
(architecture.md §6, same Next.js UI wrapped in Tauri), so opening the web UI is equally valid
and doesn't require reasoning about per-OS app-launch mechanics or whether the app is even
installed; auto-starting the Next.js dev/prod server if it isn't already running (a real,
separate piece of process-lifecycle work, not a masked-input concern).
