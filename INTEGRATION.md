# Wiring a god to pull secrets through Portunus

Portunus is the secrets god. This doc is the copy-pasteable pattern every other
god follows to stop reading raw env/files and instead resolve its runtime
secrets through the broker. The plaintext value NEVER returns up the stack, into
logs, into the audit trail, or into an LLM/chat context — a consumer receives it
only inside a child process environment (`exec`), a `{{secret:NAME}}` boundary
substitution (`resolve`), or a 0600 env file whose *path* is printed (`inject`).

Naming: `secrets set <scope> <kind>` stores `dostal-<scope>-<kind>`, registers the
reference `{{secret:<scope>-<kind>}}`, and maps `<kind>` to its conventional env
var(s) (e.g. `slack -> SLACK_BOT_TOKEN`, `anthropic -> ANTHROPIC_API_KEY`).

## The 3-line pattern

```sh
# 1. STORE ONCE (value via stdin — never on argv, never echoed)
printf '%s' "$SOME_TOKEN" | secrets set shared slack --env shared --project pantheon

# 2a. RUNTIME (env consumer): child process gets SLACK_BOT_TOKEN in its env, nothing else sees it
secrets exec shared --keys slack -- your-service --flag

# 2b. RUNTIME (inline consumer): {{secret:shared-slack}} is substituted only in the exec'd argv
secrets resolve -- curl -s -H "Authorization: Bearer {{secret:shared-slack}}" https://slack.com/api/auth.test
```

That's the whole contract. `store once`, then `exec` (env) or `resolve` (inline)
at the point of use. For a multi-key service use `secrets inject <scope>` to get a
0600 env file path and `source` it, then delete it.

## Proven reference integration: the Slack bot token

The Slack bot token (`dostal_agent`, used by the Hermes gateway) is the first
real runtime secret routed through Portunus. The existing source
(`~/.hermes/slack.env`) is intentionally LEFT INTACT as a fallback — Portunus is
ADDED as the resolve path, not a destructive cutover.

Stored (value piped from the existing env source via stdin, never argv):

```sh
set -a; . ~/.hermes/slack.env; set +a
printf '%s' "$SLACK_BOT_TOKEN" | secrets set shared slack --env shared --project pantheon \
  --description "Slack bot token dostal_agent (mirrors ~/.hermes/slack.env; env is fallback)"
```

Proof a real consumer authenticated with the token pulled from Portunus:

```sh
$ secrets exec shared --keys slack -- sh -c \
    'curl -s -H "Authorization: Bearer $SLACK_BOT_TOKEN" https://slack.com/api/auth.test'
{"ok":true,"team":"Dostal Technology","user":"dostal_agent","team_id":"T0B8XLD36E7", ...}
```

The token value appears in NONE of: the command output, the audit log, or any
file under `~/.portunus` (only the encrypted vault blob holds it):

```sh
$ secrets audit 5           # names/handles only, value absent by construction
$ secrets verify            # audit chain: INTACT
```

## Documented wrapper for the running Slack gateway (LEFT FOR REVIEW)

The Hermes gateway currently starts by sourcing `~/.hermes/slack.env` directly
(`~/.hermes/bin/gateway-restart-slack.sh`). To route its bot token through
Portunus reversibly, source the env file first (keeps every other key —
SLACK_APP_TOKEN, SLACK_SIGNING_SECRET, MULTICA_*), then OVERLAY the
Portunus-managed `SLACK_BOT_TOKEN` on top via `secrets exec`:

```sh
#!/bin/bash
# gateway-restart-slack.sh (Portunus-overlay variant — review before enabling)
set -a; [ -f "$HOME/.hermes/slack.env" ] && . "$HOME/.hermes/slack.env"; set +a  # fallback kept
cd "$HOME/Documents/work/dostal/code/hermes-agent" || exit 1
# SLACK_BOT_TOKEN is re-supplied from the vault; if Portunus is unavailable the
# sourced env value above still stands, so the gateway never loses its token.
exec secrets exec shared --keys slack -- \
  .venv/bin/hermes gateway run --replace
```

Not cut live: the gateway is a running service and restarting it risks a Slack
outage, so this wrapper is documented for review rather than activated. When
approved, swap it in and confirm the gateway reconnects
(`tail ~/.hermes/logs/gateway-slack.log`, expect a fresh `auth.test` OK).
