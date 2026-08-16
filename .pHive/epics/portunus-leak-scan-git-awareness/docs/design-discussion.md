# Design Discussion — portunus-leak-scan-git-awareness

## 1. Git repos are a new, separate config-entry type — not another path glob

`portunus leak-scan config add-path <glob>` stays exactly as it is (plain files, incremental
watermarks). A new `portunus leak-scan config add-repo <path>` manages a SEPARATE list
(`leak-scan-repos.json`, its own lock file, following the same one-store-per-write-frequency
discipline `leak-scan-config.json`/`leak-status.json`/`leak-scan-watermarks.json` already
established). At scan time, `run_scan()` additionally: for each configured repo, runs `git log
--all -p --full-history` into a fresh temp file, feeds that file through the EXISTING
`scan_paths()` unchanged (no new matching logic), tags any resulting findings with
`source_kind="git-history"` and the repo's path, then deletes the temp file. Never incremental
for repos (§2 below) — always a full re-dump per scan run.

## 2. Full re-scan per repo per run, not incremental — a deliberate, documented tradeoff

Git history can be rewritten (rebase, force-push, filter-branch) in ways that make a byte-offset
watermark (correct for append-only log files) unsafe: a rewritten history's dump could be a
different size at any point without the existing "shrink means start over" logic reliably
catching every case a human would actually want caught. Given repo histories are orders of
magnitude smaller than the 3.4 GB `.claude` corpus that motivated incremental scanning in the
first place (portunus's own history: 296 commits, a 3.3 MB dump, scanned start-to-finish in
well under a second), a full re-dump-and-scan every run is simpler, provably correct, and fast
enough that the incremental optimization isn't worth its own correctness risk here.

## 3. Source classification: `log` / `local` / `git-history`, plus public/private for the latter

Every `Finding` (and its persisted `LeakFinding`) gains `source_kind`:

- `"git-history"` — found via a configured repo's history dump. Also carries `repo_path` and
  `repo_visibility` (`"public"` | `"private"` | `"unknown"`).
- `"log"` — found via a plain configured path glob, AND the matched file's name/extension looks
  like a log/transcript (`.log`, `.jsonl`, common shell-history filenames). A soft, named
  heuristic — documented as exactly that, not a rigorous classifier. Getting this wrong just
  means a plain file is labeled "local" instead of "log," a cosmetic miss, never a security gap
  (the finding itself, and its severity, are unaffected either way).
- `"local"` — found via a plain configured path glob and doesn't match the log heuristic.

`repo_visibility` is resolved via `gh repo view <remote> --json visibility` — the SAME posture
`updater.rs` already established for this codebase (shell out to the user's own already-
authenticated `gh` CLI, never an embedded token). Non-GitHub remotes, no remote at all, or `gh`
unavailable/unauthenticated all resolve to `"unknown"` — never a guess presented as a fact. A
`"public"` classification is the single most severity-relevant piece of information this epic
adds — surfaced prominently, not buried, in every UI/CLI/MCP rendering of a finding (§5).

## 4. `gh repo view` is called once per configured repo per scan, not once per finding

Resolving visibility is a network call; a repo can produce many findings (portunus's own leak-
scan dogfooding found one secret in 48 locations). Compute `repo_visibility` ONCE per configured
repo at the start of a scan run, cache it for that run's duration, and stamp every finding from
that repo with the same value — never N redundant network calls for N findings in one repo.

## 5. Surfacing: LeakBadge's tooltip and DetailDrawer's history both need this now

The existing tooltip ("leaked in N conversations") stays as the headline, but severity framing
should distinguish "found in a public GitHub repo" (maximally urgent) from "found in a local
log file" (still real, but a different risk class) — DetailDrawer's expandable history (already
listing `path:line` per finding, portunus-leak-visibility Story 02) gains the classification
per entry: `git-history (public: owner/repo)` / `git-history (private: owner/repo)` /
`git-history (unknown visibility: owner/repo)` / `log` / `local`.

## Self-grill

- **What if a repo has multiple remotes, or none?** No remote: `repo_path` is still recorded,
  `repo_visibility` is `"unknown"` (no remote to query). Multiple remotes: use `origin` if
  present (the overwhelming convention), otherwise the first remote git reports — documented,
  not silently arbitrary.
- **Does dumping full git history to a temp file risk leaving sensitive historical content on
  disk?** The temp file is deleted immediately after each scan run (mirroring the manual
  verification's own cleanup), same discipline `resolve_to_tempfile`'s own 0600-tempfile-caller-
  deletes-it pattern already uses elsewhere in this codebase — not a new risk class.
- **Should `add-repo` require the repo to already be a leak-scan-approved path, or is adding a
  repo itself the approval?** Adding a repo IS the approval — same posture as `add-path`
  (Story 03, portunus-leak-scan): nothing is scanned until a human explicitly configures it,
  full stop, no different for repos than for plain paths.

## Scale assessment

Medium: one new config store (repo list, its own lock), a thin git-history-dump-then-reuse-the-
existing-engine wrapper (no new matching logic), a source-classification field threaded through
Finding/LeakFinding/summarize()/CLI/MCP/API, and a UI surfacing update to two already-shipped
components (LeakBadge, DetailDrawer). No changes to the core matching/severity/escalation logic
this all sits on top of. `version_bump: minor` — new capability, no breaking change.
