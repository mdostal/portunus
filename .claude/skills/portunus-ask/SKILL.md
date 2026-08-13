---
name: portunus-ask
description: Fetch and inject a secret by describing it in plain language (e.g. "the vercel secret for mdostal.com") instead of an exact reference name. Use whenever a task needs a credential injected into an env var or a file, and you don't know (or don't want to guess) its exact Portunus reference name.
---

# Portunus Ask

Thin wrapper around `portunus ask` — the semantic front door to Portunus. It lets you request
a secret by *what it's for* instead of its exact reference name, and injects it directly at a
boundary target (an environment variable or a file) without the value ever appearing in this
conversation.

**Non-negotiable:** you never see the secret value. This skill's only job is to run the CLI
command and report success/failure — never to read, echo, or otherwise handle the injected
value.

## When to use

- You need a credential for a task ("deploy to vercel for mdostal.com") and don't know its
  exact Portunus reference name.
- You'd otherwise have to ask the user for the secret's name, or worse, its value.

## When NOT to use

- You already know the exact reference name — use `portunus inject --tags <exact tags>` or
  `portunus resolve` directly; `ask` exists for when you don't.
- You need to *add* or *rotate* a secret — that stays human-only via `portunus drop` or the
  UI's add-secret form, never through this skill.

## Usage

Run:

```bash
portunus ask "<plain-language description>" --target env --var <VAR_NAME>
```

or, to write a file instead:

```bash
portunus ask "<plain-language description>" --target file --path <path> --format env|json|yaml --key <key>
```

## Fail-closed behavior — read this before retrying

`portunus ask` never guesses. If it can't confidently resolve your request to exactly one
secret, it exits non-zero and prints a clarifying question on stderr instead of picking one.
When that happens:

1. Read the stderr message — it names either the missing information (e.g. "please specify
   which env") or the candidate references it couldn't choose between.
2. Re-run with a more specific description (e.g. add the environment: "...in prod").
3. If you still can't disambiguate, ask the user — do not retry with a guess, and do not fall
   back to constructing tags yourself unless you are certain they are correct.

A non-zero exit is Portunus refusing to guess, not a bug to work around.

## What you'll see

On success, stdout confirms the reference name and the target it was injected into — never
the value. On failure, stderr explains why (ambiguous, no match, or an adapter-level error) —
also never the value.
