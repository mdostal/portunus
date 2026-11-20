# Inbox Password Reset

`portunus reset <name>` sets a new password on a web account through the account's own inbox.
No person and no visible browser are involved (PANT-1402, GH #256).

Use it when a person created an account for an agent with a throwaway password and meant the agent
to replace it. Example: the GitHub machine account `skewbald-io`, whose email is the AgentMail
inbox `skewbald@agentmail.to`. The temporary password never has to reach Portunus. After the reset,
Portunus holds a password nobody has seen.

Supported today: **GitHub**, with the reset email read from an **AgentMail** inbox. Recipes are
small data records in `src/portunus/inbox_reset.py` (`RECIPES`), and inbox readers live in
`INBOXES`. Bluesky, n8n and Glama are the next candidates.

## Set up the reference

The reference holds the account's password. It doesn't need a value yet. Tag it:

| Tag | Meaning |
|---|---|
| `reset_email` | The account's email address. It must be an inbox Portunus can read. |
| `reset_inbox_ref` | The reference that holds that inbox's API key, e.g. an AgentMail key. |
| `reset_inbox` | Inbox kind. Default `agentmail`. |
| `reset_provider` | Recipe name (`github`). Defaults to the reference's `provider`. |

```sh
portunus reg add skewbald-io-github-password skewbald-io-github-password --provider github \
  --tag reset_email=skewbald@agentmail.to --tag reset_inbox_ref=skewbald-agentmail-key
portunus reset skewbald-io-github-password --dry-run
portunus reset skewbald-io-github-password --yes
```

## What it does, in order

1. **preflight** (read-only). The inbox answers with its key, and the provider's reset page has
   the expected form and no captcha.
2. **request**. Asks the provider to send a reset email to `reset_email`.
3. **link**. Polls the inbox (up to 180 s) for that email and pulls the reset link out of it.
   Emails older than the request are ignored, with a 2-minute margin for clock skew.
4. **stash**. The current password, if there is one, is copied to `<sm_name>.previous`, and a
   `<sm_name>.resetting` marker is set.
5. **store**. A new 32-character random password is stored as the reference's value. This
   happens **before** anything is submitted.
6. **submit**. The reset form is filled with the value read back from the vault and posted over
   plain HTTP with a cookie jar. Nothing renders, so nothing can show the value.
7. **record**. The marker is cleared. The audit log gets a `reset` entry (who and when), the
   `<provider>-password` rotation binding (e.g. `github-password`) is set to `real`, and the
   reference gets `password_reset_at` / `password_reset_by` tags. An unpinned reference is pinned
   to `backend: local`, because that is where the new value was written.

`portunus rotation run` also reaches this flow through the `github-password` provider.

## When it stops

- **Captcha or other human check** (exit code 2). The flow stops before the password changes, and
  the vault is left as it was. A person has to finish the reset by hand: open the reset email in
  the inbox, then store the password they choose with `portunus prompt`, so it never passes through
  an agent's transcript.
- **Provider refuses the new password.** The previous value is put back and the marker cleared.
- **The form was sent but no answer came back.** The new password stays stored, the previous one
  stays in `<sm_name>.previous`, and the marker stays set. The next `reset` refuses to start until
  you sign in, see which password is live, and re-run with `--force`.
- **Two-factor or device check after the submit.** The provider took the password, so the reset
  counts as done. The report says a person is needed to finish signing in.

## Before you run it

GitHub's docs don't say whether a password reset revokes personal access tokens, OAuth app tokens
or SSH keys. Web sessions may be signed out. The plan prints this warning. Afterwards, check that the
account's API token still works with `portunus liveness check <token-ref>`.

The reset link and the new password exist only in local variables. They go to the local vault and
the outbound form body and nowhere else. Steps, reports, audit entries and errors carry metadata
only, never page or email bodies.

## Limits

- The new password is stored in the local vault only. A reference pinned to another backend is
  refused.
- Recipes target plain HTML form flows. A provider that needs JavaScript to submit its reset form
  needs a browser-driven recipe, which doesn't exist yet.
- Signing in with TOTP after a reset (GH #253, `portunus otp`) is not wired into the flow yet.
