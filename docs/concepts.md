# Core Concepts

The domain vocabulary behind every Portunus command and component.

---

## Reference

A **Reference** is a named credential slot — a metadata record that says "there is a secret called `my-anthropic`, it lives at `dostal-shared-anthropic` in the vault backend, it belongs to project `dostal-shared` with env `prod`." It is never the value itself. A Reference has a name, an SM name (the backend key), scope tags (`org`/`project`/`env`/`repo`), a lifecycle state, and optional descriptive metadata — but it has no `value` field at all, structurally. This is what makes it safe to list, copy, inspect, and hand to an agent: the registry can never leak what it doesn't store.

---

## ARCA

**ARCA** is the vault abstraction layer — the component that actually retrieves a secret value from whichever backend holds it. ARCA is pluggable: each reference resolves through its own backend rather than one global choice for the whole process. Backend selection follows three levels of precedence, checked in order:

1. The reference's own `backend` override field (set via `reg add`/`retag --backend`).
2. The project's `VaultBinding` (configured with `portunus bindings set <project> --backend ...`).
3. The global `PORTUNUS_BACKEND` environment variable as the final fallback.

Real backends today: `local` (local-encrypted vault, the default) and `gcp` (GCP Secret Manager, keyless via Workload Identity Federation). All others (`aws`, `vault`, `infisical`, `doppler`, `onepassword`, `azure`) are honest stubs that fail closed with a clear error. `mock` always short-circuits the entire selection tree — used for tests and dry runs only.

---

## OSTIARIUS

**OSTIARIUS** (Latin: the gatekeeper) is the single API layer that all secret requests and deposits must pass through. It has three entry points — the `portunus` CLI, the standalone UI's API routes, and the MCP stdio server — but exactly one implementation underneath them all (`resolver.py`). Nothing reaches ARCA directly; every request goes through OSTIARIUS, which then delegates to Petitio before ARCA ever gives up a value. This single-implementation, multiple-entry-point design means the boundary guarantee holds regardless of which surface the caller uses.

---

## Petitio

**Petitio** (Latin: the request, the petition) is the approval-gate wrapper that OSTIARIUS calls before ARCA resolves anything. Its central function is `check_injectable()`, which enforces two things: lifecycle state (a `dropped`/`requested`/`revoked` reference is never injectable — fail-closed) and optional per-agent access control (policy records from `roles.py`, evaluated but only enforced when `portunus roles enforce on` has been explicitly run). Every resolve gets a `would-allow` or `would-deny` audit line regardless of enforcement state — the log reflects policy decisions even while enforcement is off. Petitio returns only metadata back up the stack; the plaintext value flows exclusively from ARCA into the boundary sink, never through the policy gate itself.

---

## Injection modes

Portunus injects a secret at the **execution boundary** — the actual outbound call — never earlier. Four modes:

- **Subprocess argv** (`resolve --exec <cmd> {{secret:ref}}`): the value is substituted into the child process's argument vector only. It never appears in this shell's history, `ps` output, or any log.
- **Environment variable** (`inject --target env --var VAR_NAME`): set in the subprocess's own environment, never exported to the parent shell.
- **File** (`inject --target file --format env|json|yaml`): written as a `0600` file in the specified format; caller is responsible for deleting it after use.
- **0600 temp file** (`resolve <template>` / `portunus_resolve_to_tempfile`): Portunus writes a temp file, prints only the **path**, and returns. The caller reads the path, uses the content, and deletes the file. The value never travels through the tool return value or stdout.

These modes exist to cover different execution shapes — a CLI command, a subprocess environment, a config file — while keeping the invariant consistent: the value never enters the model's context, a log line, or a return value.

---

## Audit chain

Every resolve decision — allowed or denied — is appended to a tamper-evident hash chain (`audit.py`). Each entry carries the reference name, the SM name, and the outcome; it never carries the value. The chain is keyed by reference name so the audit record is meaningful and inspectable without ever storing what was actually fetched. `portunus verify` walks the entire chain and confirms each entry's hash matches the previous one — proving the log has not been modified since it was written. The chain also records policy decisions (`would-allow:<reason>`, `would-deny:<reason>`) even when enforcement is off, so the audit trail reflects what access control *would have done* before it's ever activated.

---

## Fail-closed default

A freshly dropped or registered reference starts in `state=dropped` (or `state=requested` for agent-initiated placeholders). In either state, `check_injectable()` refuses resolution — the reference is not injectable until a human explicitly runs `portunus state <ref> enabled`. This is the fail-closed default: a new credential slot does nothing until deliberately activated. It prevents a race where a newly stored secret becomes injectable before an operator has had a chance to review it, and it means a misconfigured or placeholder reference never silently resolves to a wrong value — it fails loudly and immediately instead.
