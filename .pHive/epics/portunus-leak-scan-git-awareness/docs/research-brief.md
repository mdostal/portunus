# Research Brief — portunus-leak-scan-git-awareness

## 1. The ask

Immediately after manually verifying that none of the real leak-scan findings (a Google
Generative AI key, a Sanity write token, a Resend audience ID, and others) had ever touched the
portunus codebase itself — checked by dumping the full git history (`git log --all -p`, 296
commits, every branch) to a scratch file and running it through the existing leak-scan engine,
then deleting the scratch file — the user asked for this to become a real, built-in capability:
"we should have both available and able to run with portunus and it should be able to help us
see -- is it logged, is it local, is it in a code base, is it in a private codebase or a public
codebase, etc."

Two concrete asks: (a) git-repository history should be a first-class scan target, not a manual
dump-to-temp-file workaround, and (b) findings should carry source classification — log file vs.
local file vs. version-controlled codebase, and if the latter, whether that codebase's remote is
public or private (a massive severity difference: a secret sitting in a private local log is a
very different risk than one sitting in a PUBLIC GitHub repo's history).

## 2. What already exists — verified, not assumed

- **No git-awareness anywhere in `leakscan.py` or any Python module today** — confirmed via
  grep. This is genuinely new engine surface, not an extension of something partial.
- **The manual verification just performed is the actual proof of concept.** `git log --all -p
  --full-history` dumped to a plain text file, then fed unchanged through `scan_paths()` (the
  existing, tested, value-never-leaks-verified engine) via a normal glob scan path. Confirmed
  working end to end: the watermark showed `offset == size` after the run, proving the full
  75,786-line dump was read to completion, not truncated. This is the mechanism to formalize,
  not reinvent — git history scanning does NOT need a new diffing/blob-walking engine; it needs
  a thin wrapper that generates the dump and feeds the same `scan_paths()` unchanged.
- **`gh repo view --json visibility,nameWithOwner,url` is the exact tool for public/private
  classification** — confirmed live (`mdostal/portunus` → `"visibility":"PUBLIC"`). This mirrors
  an EXISTING precedent already in this codebase: `ui/src-tauri/src/updater.rs` already shells
  out to `gh release view`/`gh release download` for the desktop app's self-updater, using the
  user's own already-authenticated `gh` credential, never an embedded token (`updater.rs`'s own
  docstring: "the repo is private; a public signed-feed updater would need an embedded GitHub
  token in the shipped app, which is exactly the credential-in-a-binary anti-pattern this whole
  project exists to prevent"). This story reuses that exact posture in Python, not a new pattern.

## 3. The real design questions

1. **Git history is not append-only like a log file.** Rebase/force-push can rewrite it, which
   makes the byte-offset incremental watermark (built for append-only files like `.claude`
   transcripts) unsafe to apply naively — an old commit's content could disappear from a fresh
   `git log -p` dump without the file "shrinking" in a way the existing shrink-detection would
   necessarily catch correctly (a rewritten history could be a DIFFERENT size, larger or
   smaller, at any point). Repo histories are also far smaller than the 3.4 GB `.claude` corpus
   that originally motivated incremental scanning — a full re-dump-and-scan per repo, per run,
   is an acceptable, simpler, provably-correct v1 tradeoff.
2. **"Logged" vs. "local" vs. "codebase" is a real taxonomy, not just "in a repo or not."** The
   user named three categories. A file that's part of a git repo is unambiguous ("codebase" +
   public/private). A file that ISN'T part of any repo needs a second-order distinction: does it
   look like a log/transcript (`.jsonl`, `.log`, shell history) versus some other local file? This
   is a soft heuristic, not a rigorous classification — worth documenting as exactly that, not
   oversold as certain.
3. **Public/private is GitHub-only in v1, with an honest "unknown" for everything else.** `gh
   repo view` only works for GitHub-hosted remotes with an authenticated `gh` CLI available (the
   same dependency `updater.rs` already has). A non-GitHub remote (GitLab, Bitbucket, a bare
   self-hosted repo) or an unauthenticated environment should report `"unknown"`, never guess.
