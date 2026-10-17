---
name: portunus-prompt
description: Have the HUMAN type a secret (sudo password, master credential, anything they haven't pasted to you) straight into Portunus through a desktop dialog or terminal window, without the value ever entering your context. Use whenever someone wants to store a secret "through the agent" that they have not already shared in chat.
---

# Portunus Prompt

Thin wrapper around `portunus_prompt` (MCP) or `portunus prompt` (CLI). You trigger it;
the human answers in a window on their own desktop; Portunus stores the answer in the
local vault. All you get back is metadata. Use this instead of `portunus-drop` whenever
you do **not** already hold the value.

## When to use

- "Let me store my sudo password through you", "save my master password in Portunus",
  "I'll type the key in, don't look at it".
- Any credential the human hasn't pasted into the conversation. Don't ask them to paste
  it so you can use `portunus_drop`, and don't ask them to type a shell one-liner.

## When NOT to use

- The human already pasted the value to you: use `portunus-drop` (the value is already
  in your context, prompting again adds nothing).
- There's no desktop session (headless server, SSH without X forwarding): the tool
  returns an error saying so. Tell the human to run
  `portunus drop <name> <sm_name>` at their own terminal, which prompts masked and twice.

## Usage

```
portunus_prompt(
    name="sudo-pw",                    # reference name
    sm_name="dostal-sudo",             # vault key
    provider="linux",                  # optional metadata
    description="sudo on workstation", # shown to the human in the prompt
    enable=True,                       # optional: make it injectable right away
    gate=True,                         # optional: require `portunus approve` per use
)
```

CLI: `portunus prompt sudo-pw dostal-sudo --provider linux --description "sudo on workstation" --enable --gate`
(prints one JSON object; exit 0 stored, 2 cancelled, 1 error).

The call **blocks** until the human answers, cancels or the timeout (default 300 s)
passes. Tell the human a window is about to open before calling it.

`kind` defaults to `password`, which keeps leading/trailing whitespace exactly as typed.
`confirm=True` asks twice in dialog surfaces; terminal windows always ask twice.

Surfaces (auto-selected in this order): macOS `osascript` dialog; Linux `kdialog`,
`zenity`, a GUI `pinentry`; then a new terminal window (konsole, gnome-terminal, xterm,
Terminal.app) running a masked prompt. `surface="ui"` instead creates a `requested`
placeholder and opens the dashboard's fulfill page; it returns `{"pending": true}`
immediately, so check `portunus status <name>` later.

## What you'll see

- `{"name", "state", "stored": true, "gated", "surface"}`: stored. Report it by name.
- `{"cancelled": true}` (maybe with `"reason": "timeout"`): nothing stored. Ask the human
  whether to try again; don't retry in a loop.
- `{"error": "..."}`: nothing stored; the message says why.

Never the value, its length or a hash, on any path.

## Using it afterwards (e.g. sudo)

Pair a gated reference with a wall-clock approval: the human runs
`portunus approve sudo-pw --ttl 2h`, and the reference resolves for two hours
(a bare number such as `--ttl 3` still means audit-clock ticks).
