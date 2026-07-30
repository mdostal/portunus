# Portunus — Vision

Portunus is the **secret path every god calls**. Its job is a single, unmovable invariant: a plaintext
secret never enters an LLM/agent context, a log line, the board, or a value returned up the stack. The
model sees a name; the harness injects the value at the boundary and drops it.

This document is the trajectory. Pick a rung.

---

## ① Current — where it is *today*

A **local-first secret-broker CLI + Python library**, run from a clone or `pipx`. This is real and it runs;
it is not yet wired into a running Pantheon host.

**What works now (on `main`):**
- **OSTIARIUS** — the `portunus` CLI and the `Resolver`. `resolve --exec` (value only in the child argv) and
  temp-file mode (`0600`, prints the *path* not the value) both work end-to-end against a real backend.
- **Registry** — JSON-backed, persisted `0600` under `$PORTUNUS_HOME/registry.json`. Records `name → SM
  location` plus scope/kind/state/gate. **No value field** — safe to read, copy, inspect.
- **Petitio** — the `Broker`: lifecycle guard (`dropped`/`revoked` fail closed), gate/approve with a
  time-boxed (audit-clock-ticked) approval token, and audited `grant`.
- **Audit** — a tamper-evident SHA-256 hash chain; `portunus verify` proves it is intact. Records reference
  and SM names only, never values.
- **ARCA backends** — `LocalEncryptedBackend` (the Stage 1 default, `cryptography`'s Fernet recipe, no cloud
  dependency), `MockBackend` (tests / `PORTUNUS_BACKEND=mock` dry-runs), and `GcloudBackend`, which shells to
  the `gcloud` CLI for GCP Secret Manager (Stage 2+).
- **37 passing tests**, including the load-bearing one: a resolved value must never appear in a return
  value, the audit log, or a non-`0600` file.

**Where it lives / runs:** local only. All state under `PORTUNUS_HOME` (default `~/.portunus`, `0700`).
No network dependency by default — the local-encrypted tier is the out-of-the-box backend; the GCP path
additionally needs the `gcloud` CLI + a project.

**Honest stubs / not-yet:**
- **No daemon and no HTTP server.** `manifest.json` declares a Janus **Vault** tab at `http://localhost:7802`,
  but that UI/service does **not exist yet** — the mount is a scaffold, not a running server.
- **Keyless WIF auth (DOS-81)** still lives on an **unmerged branch** (`dos-81-keyless-wif`); the
  local-encrypted vault tier (DOS-448) and secret-ignore hardening (DOS-78) have both landed on `main`.
- **Not yet mounted in the host.** Registering the manifest with a live Pantheon/Multica host and having a
  real god resolve a real secret through OSTIARIUS as its standard path is the immediate next integration
  (see Goals).

---

## ② Goals — near-term next steps

- **Daemon auto-pull on god startup.** A small resident process so that when a god boots, the secrets it
  declares are pre-resolved through Portunus at its execution boundary — no god ever handles a raw value or
  reaches a vault directly.
- **Janus Vault tab.** Stand up the `:7802` service the manifest already points at: inspect references,
  lifecycle state, and gate status; approve gated references; browse the audit chain — all **names only**,
  never values.
- **Keyless WIF auth (DOS-81)** merged from its branch, after the required human/security review of the
  IAM trust boundary.
- **Metrics + decision records.** Every resolve / gate / approve / deny emits a decision record and a metric,
  the same as every other god, so the secret path is observable and gate friction is measurable.

---

## ③ Long-term vision

**Multiple vault backends behind one ARCA interface.** GCP Secret Manager, HashiCorp Vault, and 1Password
(and more) all answer the same one dangerous question — *"give me the plaintext for this name"* — so the
backend is a swappable slot. A client on Vault, a personal workspace on 1Password, and shared infra on GCP
Secret Manager all resolve through the *same* `{{secret:NAME}}` path, with the *same* gate, the *same* audit
chain, and the *same* boundary-only guarantee. No caller — and certainly no model — ever knows or cares
which vault is underneath.

That is the whole thesis: **Portunus is the one secret path every god calls, and secrets never touch the
model.** One place to prevent a leak, one place to audit it, one interface to swap the store beneath.

**Platform direction — everything is swappable, everything is measured.** In line with the rest of Pantheon,
the vault backend (like every language, model, plugin, and god) is a toggle: you can switch the ARCA backend
on or off and **compare the metrics** — resolve latency, gate friction, failure modes — at every step. The
right backend is the one the numbers justify, not the default someone happened to wire in.

---

## Good first contributions

- **A new ARCA backend.** Implement the `SecretBackend` protocol (one method: `access(sm_name) -> str`) for
  HashiCorp Vault, 1Password, AWS Secrets Manager, or `.env`-file dev mode. `MockBackend`/`GcloudBackend` in
  `backend.py` are the model to copy.
- **Merge-forward help on the outstanding feature branch** — dogfood `dos-81-keyless-wif` and help push its
  PR through the required security review into `main`.
- **CLI ergonomics** — `portunus reg import` from an existing `.env` or a `bin/secrets` manifest; richer
  `status` output; shell-completion.
- **Audit tooling** — `portunus audit --json`, a filter by actor/secret/result, or an export.
- **Tests that tighten the invariant** — more adversarial cases proving no value escapes to a return value,
  a log, or a world-readable file; property-based fuzzing of the placeholder grammar.
- **Docs** — a worked "wire Portunus into a god's boundary call" walkthrough once the daemon lands.

New to the repo? Start with `resolver.py` (the core) and `tests/test_resolver.py` (the invariant), then run
`pytest -q`.
