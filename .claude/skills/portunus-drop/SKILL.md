---
name: portunus-drop
description: Store a new secret (a value the user just gave you, or many at once) into Portunus's local vault. Use whenever a human hands you a credential, API key, or password and asks you to save/vault/remember it -- never store it anywhere else, and never echo it back once it's saved.
---

# Portunus Drop

Thin wrapper around `portunus_drop`/`portunus_drop_bulk` (MCP) or `portunus drop`/
`drop-bulk` (CLI) — the one place in Portunus where a secret value flows *in* from your
own context rather than out of it. That's inherent to being handed a brand-new secret to
store; it is not a boundary violation the way returning a value would be. Your
responsibility on this path is narrower but still absolute: never echo the value back to
the human or your own output after a successful store, never log it, never write it
anywhere Portunus itself doesn't manage.

**Local-vault only.** This creates a secret in the local encrypted vault — it does not
create anything in GCP Secret Manager or any other cloud provider. If the active backend
is `gcloud`/`aws`, the store fails closed with a clear error rather than silently doing
nothing.

## When to use

- A human pastes you an API key/password/token and says "vault this," "save this in
  Portunus," "remember this key," or similar.
- You're importing many credentials at once (e.g. a batch of candidate
  passwords/keys someone wants tried against something later) — use the bulk form.
- You're setting up a new project's secrets from scratch and want them organized with
  real metadata (`group`/`tags`) from the start, not organized later.

## When NOT to use

- You don't actually have the value yet — don't guess, don't generate one yourself.
  Portunus is a broker, not a credential generator.
- The secret already exists in GCP Secret Manager and you just need to *reference* it —
  use `portunus discover`/`portunus_discover` to register a pointer to it instead; don't
  re-enter a value that's already stored elsewhere.
- The secret should live in the cloud, not locally — Portunus has no write path into GCP
  Secret Manager yet. Say so plainly rather than silently storing it locally instead.

## Usage — single secret

```
portunus_drop(
    name="<short reference name>",       # e.g. "gig-tracker-stripe-key"
    sm_name="<vault key>",                # e.g. "STRIPE_KEY"
    value="<the value you were given>",
    project="<project slug>",             # optional but recommended
    group="<hierarchical path>",          # optional, e.g. "gig-tracker/stripe"
    description="<what this is>",
    purpose="<what it's for>",
)
```

Or from the CLI: `portunus drop <name> <sm_name> --stdin` (value piped in, never an argv
flag) or `--value-file <path>`.

This lands the reference at `state=dropped` — fail-closed, not yet injectable. Follow up
with `portunus_state(name, "enabled")` (or `portunus state <name> enabled`) once you're
sure it's ready to be used.

## Usage — many at once

```
portunus_drop_bulk([
    {"name": "candidate-1", "sm_name": "CAND_1", "value": "..."},
    {"name": "candidate-2", "sm_name": "CAND_2", "value": "..."},
    ...
])
```

Or `portunus drop-bulk entries.json` (a JSON file, same entry shape). Returns
`{"created": [names], "failed": [{"name", "error"}]}` — a malformed entry doesn't abort
the rest of the batch.

## After you've dropped a secret

If the project doesn't yet have a vault binding configured (local vs. GCP, sync mode),
consider the `portunus-vault-setup` skill next — especially before creating a first
secret in a brand-new project.

## What you'll see

On success: `{"name", "sm_name", "state"}` (single) or `{"created", "failed"}` (bulk) —
never the value, on any path. On failure: a clear `error` string, still never the value.
Report success/failure to the human by name only — do not repeat the value back, even to
confirm it "looks right."
