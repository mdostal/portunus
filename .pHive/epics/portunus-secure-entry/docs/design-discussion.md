# Design discussion: portunus-secure-entry

## 0. Goal

Make handing Portunus a secret value easy and safe when a human is working alongside an agent:
mask it, trim it, and give an agent a way to open the entry surface for the human without ever
seeing the value itself — building on real, already-shipped mechanisms (`AddSecretForm`/
`/api/drop`, `Registry.request()`'s `state=requested` placeholders) rather than inventing a
parallel path.

## 1. Whitespace trimming

**Decision: `str.strip()` (both ends, all whitespace — spaces, tabs, `\r`, `\n`), applied once,
at `cmd_drop`'s own read.** Both `--stdin` and `--value-file` funnel through the same `value =
...` assignment in `cmd_drop` — trimming there is the single choke point; the web UI's
`/api/drop` route pipes to this same command, so it inherits the fix for free without its own
duplicate trim logic (one implementation, not two — the pattern this codebase uses everywhere
else, e.g. `--tags` parsing via one shared `_parse_tags()`).

**Self-grill: could a real secret legitimately need leading/trailing whitespace?** In
principle, yes (vanishingly rare in practice — no mainstream API key/token format uses it
meaningfully). Every other mainstream secret-entry surface (`kubectl create secret`,
`op item create`, GitHub's own "New secret" UI) trims by default for exactly this reason: silent
invisible corruption from copy-paste is a far more common and far worse failure mode than the
near-nonexistent case of a whitespace-significant secret. No opt-out flag is added for this
v1 — if a real user ever needs one, it's a one-line follow-up, not a redesign.

## 2. Interactive masked CLI prompt

**Decision: when `cmd_drop` is invoked with neither `--stdin` nor `--value-file`, prompt
interactively via `getpass.getpass()` (stdlib, cross-platform, echoes nothing to the terminal)
— twice, requiring both entries to match before proceeding.** This is the direct answer to "a
TUI that obfuscates and hides it": a human sitting at their own terminal (not the agent's tool
channel — an agent's Bash-tool call has no TTY; `getpass()` against closed/empty stdin raises
`EOFError` immediately (verified directly: redirecting from `/dev/null` raises at once, no hang),
which is
exactly why this only ever helps when a *human* runs the command themselves, per
research-brief.md §1) types the value with nothing echoed, gets a chance to catch a typo before
anything is stored (masked input has zero visual feedback, so a typo-catching second entry is
cheap insurance — the same reason `passwd`/`ssh-keygen` do it), and the trimmed, confirmed value
is stored exactly the same way `--stdin`/`--value-file` already do (through the same `value =
...` variable, same `del value` scrub, same `backend.store()` call — no parallel code path).

**Self-grill: does this change any existing `--stdin`/`--value-file` behavior?** No — this is a
strictly additive third mode, gated on *neither* flag being present. Every existing script/CI
usage of `--stdin`/`--value-file` is untouched.

**Self-grill: what if `getpass()` can't suppress echo (no real TTY, e.g. piped into another
program)?** `getpass` already handles this itself — it falls back to a visible `input()` with a
printed warning on a non-TTY stream, never crashes. No new handling needed; this is stdlib
behavior this story relies on rather than reimplements.

## 3. Wiring `state=requested` into the existing dashboard

**Decision: `DetailDrawer` gains a "Fulfill" action (visible only when `reference.state ===
"requested"`, alongside the existing `onRotate` action) that opens `AddSecretForm` pre-filled
from that reference's own metadata — name, sm_name, org, provider, project, env, tags,
description, purpose, injected_as, group, related, repo, source_files — mirroring exactly how
`rotateDraft` already pre-fills the form today (`page.tsx`), just from a different source
object and a different trigger button.** No new component — `AddSecretForm` already accepts a
partial `initial` draft; a `state=requested` reference is a strict superset of what a rotate
draft supplies today.

**Also: a `?fulfill=<name>` URL query param, read on page load, auto-opens `AddSecretForm`
pre-filled for that reference (same pre-fill path as the button above) if the name exists and
is `state=requested`; a clear inline error (not a silent no-op) if the name doesn't exist or
isn't in that state anymore (e.g. someone already fulfilled it in another tab).** This is the
deep-link Story 04 (`portunus ui open --fulfill NAME`) opens a browser tab to — the mechanism
an agent's own fire-and-forget "open a URL" call can point at.

**Self-grill: why not a dedicated "pending requests" list/tab instead of a query param?** A
list view is real, useful future work (tracked as a follow-up, not silently dropped) — but it's
a bigger, separate UI surface, and the immediate ask is "an agent can open a place to enter
this one key," which the query-param deep link answers directly and minimally. The existing
Console/ProjectExplorer already show `state=requested` references in the normal list (StatePill
renders `state-requested`) — a human browsing without an agent's link still finds them and can
use the new Fulfill button. The query param and the button share the exact same pre-fill code
path, so building the list view later is additive, not a rework.

## 4. `portunus ui open [--fulfill NAME]`

**Decision: `webbrowser.open(url)` (Python stdlib, cross-platform — macOS/Linux/Windows all
handled without per-OS branching) against `PORTUNUS_UI_URL` (default
`http://localhost:3000`, matching the documented `npm run dev` default in README.md), with
`?fulfill=<name>` appended when `--fulfill` is passed.** Before opening, if `--fulfill NAME` is
given, the command looks the name up in the local registry and fails clearly (never opens a
browser to a broken/misleading URL) if it doesn't exist or isn't `state=requested` — the same
"never silently do the wrong thing" posture the rest of this codebase already has (e.g.
`resolve_by_tags`'s `AmbiguousMatch`/`NoMatch`). A brief reachability probe (a fast, short-timeout
HTTP GET to the target URL) before opening the browser, so a human/agent gets a clear "no local
UI is running — start one: `cd ui && npm run dev`" message instead of a browser tab that just
sits on a connection-refused error.

**Self-grill: should this also try to launch the packaged desktop app?** Deliberately deferred
(research-brief.md §4) — the desktop app is the same Next.js UI wrapped in Tauri
(architecture.md §6, "packaging, not a new component"), so opening the web UI is functionally
equivalent for this ask's purpose and avoids reasoning about per-OS app-bundle launch mechanics
or install detection, which is real, separate scope. If a distinct desktop-launch UX is wanted
later, it's an additive flag on this same command, not a redesign.

**Self-grill: should this command auto-start the dev server if it isn't running?** No —
spawning and managing a long-running background process from a one-shot CLI command is a
different, real feature (process lifecycle: who owns it, when does it stop, what happens on a
second `ui open` call) that this ask doesn't ask for. The clear, actionable fallback message is
the right-sized v1.

**Self-grill: is this CLI-only, matching `vault export`/`import`/`vault access verify`'s own
"human/agent initiates a real action" posture?** Yes, though for a different reason again (§4 of
`portunus-vault-transfer`'s design-discussion already established this pattern for two
different reasons — value exposure risk, and real-API-call side effects). Here it's neither:
opening a browser tab has no meaningful side effect and no value ever flows through it. No MCP
tool is added regardless, because there's nothing an MCP tool would add over the agent just
running the CLI command directly via its normal shell-command tool — unlike `resolve`/`ask`,
this command produces no information an agent needs back.

## 5. Version bump

`minor` — three new, additive, backward-compatible surfaces (interactive drop mode, UI fulfill
wiring, `ui open` command) plus a bug fix (whitespace trim) with no breaking change to any
existing flag, return shape, or exit code.
