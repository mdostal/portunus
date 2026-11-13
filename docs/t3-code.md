# Portunus with T3 Code

T3 Code is a desktop and web app that runs coding agents such as Claude Code and Codex. It has two features that deal with secrets:

- **Secret cards.** An agent calls T3 Code's `request_secret` tool and a private card appears in the thread. The human types the value into the card; the agent gets a file path or an opaque handle, never the value.
- **Secret providers.** A card, and some settings fields, can be answered with a *reference* to a configured provider instead of a typed value. T3 Code then reads the value itself.

On their own, cards do not keep anything. The value a human types is used once, the human is asked again next time, and the file the card wrote stays on disk. Portunus connects to both features so that a secret is typed once and kept in the vault:

| Direction | What happens | How |
|---|---|---|
| Card → vault | What the human types into a card is stored in the Portunus vault, and the card's file is deleted. | The `t3` surface of [`portunus prompt`](cli-reference.md#prompt) / `portunus_prompt` |
| Vault → card or settings | The human answers a card, or fills a settings field, with `op://portunus/<name>/value`, and T3 Code reads the value from the vault. | The `op` launcher installed by [`portunus t3 install-shim`](cli-reference.md#t3-install-shim) |

In both directions the value stays out of the agent's context.

## Card → vault: the `t3` prompt surface

Nothing to set up beyond `portunus agent init`, which registers the MCP server and installs the `portunus-prompt` skill that teaches agents this flow.

Portunus cannot show a T3 Code card itself. Only the agent can call T3 Code's tool. So the surface is a handoff in two `portunus_prompt` calls, with the card in between:

1. The agent calls `portunus_prompt(name, sm_name, ...)`. Inside a T3 Code thread this stores nothing and returns a handoff that names the card tool and its arguments:

    ```json
    {"stored": false, "pending": true, "surface": "t3", "name": "sudo-pw",
     "card": {"tool": "mcp__t3-code__request_secret",
              "arguments": {"label": "sudo on workstation",
                            "reason": "The value is stored in your Portunus vault as 'sudo-pw' and the agent never sees it.",
                            "delivery": "file"}},
     "then": {"tool": "portunus_prompt",
              "arguments": {"name": "sudo-pw", "sm_name": "dostal-sudo",
                            "value_file": "<file path returned by the card>",
                            "description": "sudo on workstation"}},
     "note": "Call the card tool, then call portunus_prompt again with value_file set to the path it returned. Do not read, print or copy that file."}
    ```

2. The agent calls the card tool. You type the value into the card. T3 Code writes it to a file only you can read and gives the agent the path: `{"status": "saved", "file": "<path>"}`.

3. The agent calls `portunus_prompt` again with `value_file` set to that path. Portunus reads the file, stores the value in the local vault, deletes the file and returns `{"name", "state", "stored": true, "gated", "surface": "t3", "file_removed": true}`.

The CLI does the same: `portunus prompt <name> <sm_name> --surface t3` prints the handoff, and `portunus prompt <name> <sm_name> --value-file <path>` finishes it.

### When the card is used

`--surface auto` (the default) picks the card whenever `T3CODE_THREAD_ID` is set. T3 Code sets that variable for every agent it starts, and the Portunus MCP server and CLI inherit it. T3 Code threads are often driven from a phone or another machine, where a dialog on the server's desktop cannot be reached; the card shows up in the thread you are already reading.

To change that:

- Set `PORTUNUS_PROMPT_SURFACE` to a surface name to choose what `auto` picks. `PORTUNUS_PROMPT_SURFACE=kdialog` brings back the desktop dialog inside T3 Code. It must be set where the Portunus MCP server runs, which is the agent's environment.
- An explicit `--surface` / `surface=` always wins over both.

### What Portunus checks before reading the card's file

The file must be given as an absolute path and must be a regular file (not a symlink), owned by you, with no group or other permission bits, and between 1 byte and 64 KiB. If any of that fails, Portunus returns an error, stores nothing and leaves the file alone.

One trailing newline is removed from the value. All other whitespace is kept.

The file is deleted only after the value is in the vault. If storing fails, the file is left in place and the error says so, so that you do not have to type the value again. If the value was stored but the file could not be deleted, the result has `"file_removed": false` and a `warning` with the path to delete.

## Vault → card or settings: the `op` launcher

Stock T3 Code has one kind of secret provider, 1Password, which it uses by running the `op` command it finds on `PATH`. Portunus ships a small launcher named `op` that answers the two calls T3 Code makes, from the Portunus vault. A T3 Code provider of kind 1Password with the account `portunus` then reads from Portunus.

### Set it up

```sh
portunus agent init          # MCP server and skills, if not done already
portunus t3 enable           # allow T3 Code to read from the vault
portunus t3 install-shim     # write the `op` launcher next to `portunus`
portunus t3 status           # check, and see what to type into T3 Code
```

Then, in T3 Code, open **Settings → Secrets → Add provider** and enter:

| Field | Value |
|---|---|
| Name | `Portunus` |
| Account | `portunus` |

The provider is of T3 Code's 1Password kind, the only kind it has. The name is yours to choose; the account must be exactly `portunus`.

The launcher's directory has to be on the `PATH` of the T3 Code server. `install-shim` warns when it is not on the `PATH` of the shell you ran it from. If you had to change `PATH`, restart T3 Code.

`install-shim` refuses to run if another `op` is already on `PATH`, which normally means the 1Password CLI is installed. T3 Code runs whichever `op` comes first, so the two cannot share a `PATH`. `--force` installs anyway; use `--dir` to put the launcher in a directory that only T3 Code has on its `PATH`.

### References

A reference has the form:

```
op://portunus/<reference-name>/value
```

The first part is always `portunus` and the last is always `value`. The middle is the Portunus reference name. `portunus t3 ref <name>` prints it, ready to paste:

```sh
$ portunus t3 ref sudo-pw
op://portunus/sudo-pw/value
```

T3 Code only accepts letters, digits, `.`, `_`, `-` and inner spaces in each part. A Portunus reference whose name has other characters (a `/`, for example) cannot be read from T3 Code; `portunus t3 ref` says so.

The reference must be `enabled` to be read. A secret stored with `portunus prompt` lands `dropped` unless `--enable` was given; run `portunus state <name> enabled`.

### Guard rails

Everywhere else, Portunus hands a value to a command's arguments, a 0600 file or a callable, and never prints it. The launcher is the one place a value is written to stdout, because that is the only way T3 Code's 1Password provider accepts one. It is fenced in:

1. **Off by default.** Nothing is read until you run `portunus t3 enable`. `portunus t3 disable` turns it off again.
2. **Never to a terminal.** If stdout is a terminal, the launcher refuses.
3. **Only for T3 Code.** The process that started the launcher must look like the T3 Code server: its executable is `t3code`, `t3`, `node` or `electron`, or a name you list in `PORTUNUS_T3_PARENTS` (comma-separated). It must not be a shell or an agent CLI: `sh`, `bash`, `zsh`, `fish`, `dash`, `claude`, `codex` and any `python*` are always refused, as is `node` running Claude Code or Codex. An agent that types `op read op://portunus/...` in its own shell gets a refusal.
4. **Only the `portunus` account.** `--account` must be exactly `portunus`. A T3 Code provider that points at a real 1Password account never reaches the vault.
5. **Only `op://portunus/<name>/value`**, for a name that is in the registry.
6. **The same policy as every other read.** State, the approval gate and role policy are checked by the same code as `portunus resolve`. Nothing is cached.
7. **Audited.** Every value written adds a `t3_provider_read` entry with result `ok` to the [audit log](audit.md), next to the usual `resolve` entry. Every refusal adds `t3_provider_read` with `denied:<reason>`, prints one line on stderr and nothing on stdout, and exits 1.

!!! warning "The limits of guard rail 3"
    Checking the parent process raises the bar against an agent calling the launcher from its own shell. It is not a security boundary. A process that runs as you can arrange for an allowed parent, for example by starting `node` itself.

    The control that does hold is the approval gate. Gate any reference an agent must not be able to read on its own: `portunus gate <name>`. A gated reference cannot be read through the launcher, or any other way, until you run `portunus approve <name>`.

Reads run as the user and environment of the T3 Code server. If you keep the vault somewhere other than `~/.portunus`, `PORTUNUS_HOME` must be set for the T3 Code server too.

Each audit entry advances the audit clock. An approval given as a count (`portunus approve <name> --ttl 3`) therefore runs out after fewer reads through the launcher than through `portunus resolve`. For T3 Code, approve by time: `portunus approve <name> --ttl 2h`.

## Undo

```sh
portunus t3 disable          # the launcher refuses every read
portunus t3 uninstall-shim   # remove the launcher (only one Portunus wrote)
```

Then remove the `Portunus` provider in T3 Code's settings. To stop `portunus prompt` from using cards, set `PORTUNUS_PROMPT_SURFACE` as described above. Secrets already stored stay in the vault; remove one with `portunus reg rm <name>`.

## Troubleshooting

**The card or settings field says the provider could not read the value.** T3 Code only records the exit code, so ask Portunus:

```sh
portunus t3 status
portunus audit 100 | grep t3_provider_read
```

`status` shows whether the reader is enabled, which `op` is first on `PATH` and whether it is the Portunus launcher. The last `t3_provider_read` entry gives the reason for a refusal:

| Result | Meaning | Fix |
|---|---|---|
| `denied:disabled` | The reader is off. | `portunus t3 enable` |
| `denied:approval-required` | The reference is gated. | `portunus approve <name> --ttl 2h` |
| `denied:not-injectable` | The reference is not `enabled`. | `portunus state <name> enabled` |
| `denied:unknown-reference` | No reference has that name. | `portunus search <text>`, then `portunus t3 ref <name>` |
| `denied:reference` | The reference is not `op://portunus/<name>/value`. | `portunus t3 ref <name>` |
| `denied:account` | The provider's account is not `portunus`. | Edit the provider in T3 Code |
| `denied:parent:<name>` | The launcher was started by `<name>`, not by T3 Code. | If `<name>` is your T3 Code server, add it to `PORTUNUS_T3_PARENTS` |
| `denied:not-authorized` | Role policy denies the read. | [Access control](access-control.md) |
| `denied:backend-error` | The vault has no value under that reference, or could not decrypt it. | `portunus health` |

**There is no `t3_provider_read` entry at all.** T3 Code did not run the launcher. Either the launcher's directory is not on the `PATH` of the T3 Code server (restart T3 Code after installing it), or T3 Code and your shell use different Portunus homes.

**`portunus t3 status` says the launcher is not the Portunus launcher.** Another `op`, usually the 1Password CLI, comes first on `PATH`. See [Set it up](#set-it-up).

**`portunus prompt` opens a desktop dialog inside T3 Code, or the card when you wanted the dialog.** Check `T3CODE_THREAD_ID` and `PORTUNUS_PROMPT_SURFACE` in the agent's environment; `portunus t3 status` reports which surface `auto` picks.

**The agent says the card's file was rejected.** The error names the check that failed (see [What Portunus checks](#what-portunus-checks-before-reading-the-cards-file)). The file is still there; delete it once you no longer need it.
