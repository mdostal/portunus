---
name: portunus-vault-setup
description: Configure which vault backend a project's secrets use (local-only, GCP Secret Manager, or GCP with a local sync-down cache), check current bindings, or force a cache refresh. Use when setting up a new project's secrets, when a project needs mixed local/cloud storage, or when a deploy needs a guaranteed-fresh set of secrets.
---

# Portunus Vault Setup

Thin wrapper around `portunus_bindings_show`/`portunus_sync` (MCP) or `portunus bindings
show/set`/`portunus sync` (CLI). Portunus routes each secret to a backend by a 3-level
precedence: a reference's own `backend` override, else its project's binding, else a
process-wide default. This skill covers the middle level — per-project configuration —
which is almost always the right level to set things at.

## When to use

- Setting up secrets for a brand-new project and deciding where they should live:
  entirely local (personal projects, sensitive local-only credentials), entirely GCP
  (production secrets, team-shared), or GCP-backed-with-local-caching (frequently
  accessed secrets where you don't want a live Secret Manager round-trip every time).
- A human says something like "keep the gig-tracker stuff local," "this project's
  secrets should sync from GCP," or "make sure the deploy has fresh secrets."
- Checking what's currently configured for a project before creating secrets in it (a
  cleaner default than guessing, and prevents accidentally creating a local secret in a
  project a human expected to be GCP-backed).

## When NOT to use

- You want a *specific secret* (not a whole project) to use a different backend than its
  project's default — set `backend` directly on that reference instead (via
  `portunus_drop`'s/`reg add`'s `backend` parameter, or `retag --backend`), not here.
- You're asking Portunus to actually create something in GCP Secret Manager — it can't;
  bindings only say where *existing* GCP secrets get discovered/fetched from, and where
  local-only secrets get stored. See `portunus-drop` for local creation.

## Checking current configuration

```
portunus_bindings_show(project="<project>")   # one project
portunus_bindings_show()                       # every configured project
```

Reports `backend` (`local`/`gcp`/`aws`), `sync_mode` (`direct`/`cached`), and (for GCP)
`account`/whether a WIF audience is configured — never the audience value itself.

## Setting a project's backend/sync mode

```
portunus bindings set <project> --backend local
portunus bindings set <project> --backend gcp --sync-mode direct     # live-fetch every access (default)
portunus bindings set <project> --backend gcp --sync-mode cached     # pull-only sync-down cache
```

Only explicitly-passed fields change — an existing `account`/`wif_audience` on that
project is preserved automatically.

`sync_mode="cached"` is for projects where secrets are accessed often and don't rotate
often: Portunus fetches from GCP once, caches the value locally (encrypted, same as a
directly-dropped secret), and only re-fetches when the remote value has actually
changed. Never syncs the other direction — a local secret never gets pushed to GCP.

## Forcing a fresh sync

```
portunus_sync("<project>")   # or: portunus sync <project>
```

Forces a recency check (and re-fetch if stale) for every cached-mode reference in that
project right now, rather than waiting for the next incidental access. Useful before a
deploy that wants a guaranteed-fresh set of secrets materialized once. Returns
`{"synced", "already_fresh", "failed"}` — reference names and error strings only, never a
value.

## What you'll see

Bindings/sync tool output is metadata only — backend names, sync modes, account emails,
reference names, boolean/presence flags. Never a secret value, never a WIF audience
string. Report configuration changes and sync results by name; there's nothing here that
needs to stay hidden from the human the way a secret value does.
