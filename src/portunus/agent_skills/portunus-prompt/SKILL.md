---
name: portunus-prompt
description: Have the HUMAN type a secret (sudo password, master credential, anything they haven't pasted to you) straight into Portunus through a desktop dialog, a terminal window or, inside T3 Code, a T3 Code secret card, without the value ever entering your context. Use whenever someone wants to store a secret "through the agent" that they have not already shared in chat, and before calling T3 Code's request_secret for a value that should be kept.
---

# Portunus Prompt

Thin wrapper around `portunus_prompt` (MCP) or `portunus prompt` (CLI). You trigger it;
the human answers in a window on their own desktop, or in a T3 Code secret card; Portunus
stores the answer in the local vault. All you get back is metadata. Use this instead of
`portunus-drop` whenever you do **not** already hold the value.

**Check the vault first.** Run `portunus_search` (or `portunus_ask_preview`) for the secret
before you prompt. Only prompt when it is missing. A human who already stored their sudo
password should not be asked for it again.

## When to use

- "Let me store my sudo password through you", "save my master password in Portunus",
  "I'll type the key in, don't look at it".
- Any credential the human hasn't pasted into the conversation. Don't ask them to paste
  it so you can use `portunus_drop`, and don't ask them to type a shell one-liner.

## When NOT to use

- The human already pasted the value to you: use `portunus-drop` (the value is already
  in your context, prompting again adds nothing).
- The secret is already in the vault: use it (see "Using it afterwards").
- There's no desktop session (headless server, SSH without X forwarding) and you are not
  in T3 Code: the tool returns an error saying so. Tell the human to run
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

Outside T3 Code the call **blocks** until the human answers, cancels or the timeout
(default 300 s) passes. Tell the human a window is about to open before calling it.
Inside T3 Code it returns at once with a handoff; see "Inside T3 Code" below.

`kind` defaults to `password`, which keeps leading/trailing whitespace exactly as typed.
`confirm=True` asks twice in dialog surfaces; terminal windows always ask twice.

Surfaces (auto-selected in this order): macOS `osascript` dialog; Linux `kdialog`,
`zenity`, a GUI `pinentry`; then a new terminal window (konsole, gnome-terminal, xterm,
Terminal.app) running a masked prompt. `surface="ui"` instead creates a `requested`
placeholder and opens the dashboard's fulfill page; it returns `{"pending": true}`
immediately, so check `portunus status <name>` later. `surface="t3"` is the T3 Code
secret card.

## Inside T3 Code

In a T3 Code thread (`T3CODE_THREAD_ID` is set) `surface="auto"` picks `t3`: the human
may be on a phone or another machine, where a dialog on the server's desktop is out of
reach, and the card appears in the thread they are already reading. Portunus cannot raise
a T3 Code card itself, so you make three tool calls. The value never passes through you.

1. Call `portunus_prompt` as usual. Nothing is stored yet. It returns a handoff:

   ```
   portunus_prompt(name="sudo-pw", sm_name="dostal-sudo",
                   description="sudo on workstation", enable=True, gate=True)
   -> {"stored": false, "pending": true, "surface": "t3", "name": "sudo-pw",
       "card": {"tool": "mcp__t3-code__request_secret",
                "arguments": {"label": "sudo on workstation",
                              "reason": "The value is stored in your Portunus vault as 'sudo-pw' and the agent never sees it. Each use will need your approval.",
                              "delivery": "file"}},
       "then": {"tool": "portunus_prompt",
                "arguments": {"name": "sudo-pw", "sm_name": "dostal-sudo",
                              "value_file": "<file path returned by the card>",
                              "description": "sudo on workstation",
                              "enable": true, "gate": true}}}
   ```

2. Call the card tool with exactly the arguments in `card`:

   ```
   mcp__t3-code__request_secret(label="sudo on workstation", reason="...", delivery="file")
   -> {"status": "saved", "file": "/path/to/the/card/file"}
   ```

   **Never read, print, copy or `cat` that file.** It holds the value.

3. Call `portunus_prompt` again with the arguments in `then`, putting the card's path in
   `value_file`:

   ```
   portunus_prompt(name="sudo-pw", sm_name="dostal-sudo", value_file="/path/to/the/card/file",
                   description="sudo on workstation", enable=True, gate=True)
   -> {"name": "sudo-pw", "state": "enabled", "stored": true, "gated": true,
       "surface": "t3", "file_removed": true}
   ```

   Portunus stores the value and deletes the file. If the result has
   `"file_removed": false`, delete the path yourself (`rm`), still without reading it.
   If the result is an error, the file is left in place so the human does not have to
   retype: fix what the error names and repeat step 3, or delete the file.

CLI form: `portunus prompt sudo-pw dostal-sudo --surface t3` prints the same handoff (plus
the matching command line under `then.cli`), and
`portunus prompt sudo-pw dostal-sudo --value-file /path/to/the/card/file` finishes it.

Rules inside T3 Code:

- **Never call `request_secret` for a value that should persist without routing it into
  the vault.** A card on its own stores nothing: the human is asked again next time, and
  the file stays on disk. Start from `portunus_prompt` and let it tell you which card to
  raise. A bare card is only right for a value that is used once and then thrown away.
- If the human dismisses the card, stop. Do not call `portunus_prompt` a second time and
  do not raise the card again unless they ask.
- The human can make `auto` use the desktop dialog again with
  `PORTUNUS_PROMPT_SURFACE=kdialog` (or any other surface name). Passing `surface=`
  yourself always wins.

## What you'll see

- `{"name", "state", "stored": true, "gated", "surface"}`: stored. Report it by name.
- `{"stored": false, "pending": true, "surface": "t3", "card", "then"}`: nothing stored
  yet. Follow "Inside T3 Code".
- `{"cancelled": true}` (maybe with `"reason": "timeout"`): nothing stored. Ask the human
  whether to try again; don't retry in a loop.
- `{"error": "..."}`: nothing stored; the message says why.

Never the value, its length or a hash, on any path.

## Using it afterwards (e.g. sudo)

Pair a gated reference with a wall-clock approval: the human runs
`portunus approve sudo-pw --ttl 2h`, and the reference resolves for two hours
(a bare number such as `--ttl 3` still means audit-clock ticks).

For a command that reads the secret on stdin, such as `sudo -S`:

1. `portunus_resolve_to_tempfile(name="sudo-pw")` returns `{"path": "<file>"}`: a 0600
   file holding the value. Do not read it.
2. Run the command with the file as its stdin: `sudo -S -p '' <cmd> < "$file"`.
3. Delete the file straight away: `rm -f "$file"`. Do it even when the command failed.

If step 1 answers that the reference needs approval, ask the human to run
`portunus approve sudo-pw --ttl 2h`; do not prompt for the password again.
