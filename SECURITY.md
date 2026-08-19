# Security Policy

Portunus is a secret broker — a security bug here is more consequential than in most projects.
If you find one, please report it privately rather than opening a public issue.

## Reporting a vulnerability

Use GitHub's private vulnerability reporting:
[**Report a vulnerability**](https://github.com/mdostal/portunus/security/advisories/new).
This opens a private advisory visible only to you and the maintainer — nothing is public until
a fix ships and you both agree it's ready to disclose.

Please include:

- What you found and why it matters (what could an attacker actually do?)
- Steps to reproduce, or a minimal proof of concept
- Which version/commit you tested against

You should get an initial response within a few days. There's no bug bounty — this is a
solo-maintained open-source project — but real reports get real fixes and public credit
(unless you'd rather stay anonymous) in the fix's changelog entry.

## What counts as a security issue here

The project's one non-negotiable invariant: **a resolved secret value must never appear in a
return value, log line, print statement, exception message, or audit-chain entry, on any code
path — including failure paths.** Anything that breaks this invariant is a security issue,
however it happens: a new code path that returns a value, an error message that echoes one, a
log line that captures one, etc.

Also in scope: anything that lets a caller resolve/inject a secret it shouldn't have access to,
anything that weakens the audit chain's tamper-evidence, and anything in the keyless-auth paths
(GCP Workload Identity Federation) that could mint a credential it shouldn't.

**Out of scope, by design — not a vulnerability report:** a command *you* wrap with
`resolve --exec`/`resolve_exec` echoing its own argument (`echo {{secret}}` will print the
secret, because that's the wrapped command's own behavior, not Portunus's — see README's
"Honest scope" section). Also out of scope: the several backends that are documented, honest
stubs (they unconditionally fail closed, by design, until someone implements them for real).

## Supported versions

This project is pre-1.0 and moves fast. Only the latest released version is supported —
please upgrade (`portunus update run`) before reporting, if you can.
