## Audit log and chain verification

Every access decision Portunus makes is recorded in a tamper-evident hash chain. The log is append-only; any deletion or modification breaks the chain in a way `portunus verify` detects.

---

## What is logged

Every event recorded to the audit log follows this shape:

| Field | Contents |
|-------|----------|
| `seq` | Monotonic sequence number (from a `.clock` file, never a wall clock) |
| `actor` | The identity making the request (`DOSTAL_AGENT` env var for agents, `USER` for humans) |
| `task` | The task identifier (`DOSTAL_TASK` env var, if set) |
| `action` | What happened: `resolve`, `drop`, `approve`, `gate`, `grant`, `retag`, `session_store`, `session_load`, etc. |
| `secret` | The **reference name or SM name** being acted on — never the secret value |
| `result` | Outcome string, e.g. `ok:env:MY_VAR`, `denied-dropped`, `would-allow:no-policy-configured` |
| `prev` | The hash of the immediately preceding entry (or `"genesis"` for the first entry) |
| `h` | SHA-256 of `prev` + the current entry's JSON body |

**The secret value, raw credentials, and caller environment variables are never written.** The `secret` field contains the reference or SM name only — the metadata that describes what was accessed, not what the value is.

---

## How the hash chain works

Each entry's hash `h` is computed over the previous entry's hash (`prev`) plus the current entry's JSON body:

```
h = SHA-256(prev_hash + json_body)
```

The `prev` field of each entry carries the previous `h`, forming a chain. The chain root uses the literal string `"genesis"` as the initial `prev`. Any edit to any entry — even changing a single character — produces a different `h` that no longer matches the next entry's `prev`, making the tampering detectable.

---

## Browsing the log

```bash
# Show the last 20 entries (default)
portunus audit

# Show only entries for a specific secret reference
portunus audit --secret my-sm-name

# Show more entries
portunus audit --n 100

# Output as JSON
portunus audit --json
```

Each line shows: sequence number, actor, action, secret name, and result.

---

## Verifying the chain

```bash
portunus verify
```

Output is one of:

- `audit chain: INTACT (N entries)` — the hash chain is unbroken; exit code 0.
- `audit chain: BROKEN (N entries)` — at least one entry's hash does not match; exit code 2.

Run this after a vault restore or whenever you suspect tampering. A broken chain does not tell you _which_ entry was modified — it signals that the chain cannot be trusted from that point forward.

---

## What is NOT logged

The following are deliberately excluded from every audit entry:

- **Secret values** — the plaintext of any managed credential.
- **Raw credentials** — refresh tokens, access tokens, OAuth client secrets.
- **Caller environment variables** — shell env at the time of the resolve.
- **The WIF audience or access token** — only the identity and scope metadata of a minted token appear in auth events, never the token itself.

The audit log is safe to share with an administrator for compliance review without risk of exposing any managed secret.
