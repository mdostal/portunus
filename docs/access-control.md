## Per-agent access control (Petitio)

Petitio is Portunus's policy layer. It sits between every resolution request and the vault backend, enforcing two kinds of gates: a lifecycle/approval gate (always active) and a role-based access control (RBAC) system (configurable, default off).

---

## Default behavior: fully open

A reference with no configured policy is always resolvable by any caller. This is the "permissive-if-unconfigured" posture: enforcement only ever narrows access for a scope that has at least one policy record. A fresh vault has no records — every resolve succeeds.

---

## Role-based access control

RBAC policies persist in `PORTUNUS_HOME/roles.json` (0600). Each policy record covers one scope dimension and one principal.

### Scope dimensions

Policies match references by one of four dimensions:

| Scope type | Matches |
|------------|---------|
| `org` | References whose `org` field equals the scope value |
| `project` | References whose `project` field equals the scope value |
| `env` | References whose `env` field equals the scope value |
| `repo` | References whose `repo` field equals the scope value |

### Setting a policy

```bash
portunus roles set \
  --scope-type project \
  --scope-value my-project \
  --role dev \
  --actions read,test \
  --principal my-agent-name
```

`--principal` names which agent or identity the policy applies to. Omit it (or pass `*`) to apply the record to everyone. Two records with the same scope and role but different principals are stored independently.

`--actions` is a comma-separated list of free-text strings; Portunus records them as-is and does not interpret them at enforcement time — they exist for documentation and for a future policy engine to act on.

### Viewing policies

```bash
# Show all configured policies
portunus roles show

# Filter by dimension
portunus roles show --scope-type project --scope-value my-project
```

### Deleting a policy

```bash
portunus roles delete \
  --scope-type project \
  --scope-value my-project \
  --role dev \
  --principal my-agent-name
```

---

## Activating enforcement

By default, policy evaluation runs in **audit-only mode**: every resolve logs a `would-allow` or `would-deny` result to the audit chain but never raises an error. This lets you review the audit log before committing to enforcement.

Turn enforcement on or off:

```bash
portunus roles enforce on
portunus roles enforce off
portunus roles enforce status   # check current state
```

When enforcement is on, a `would-deny` decision raises `NotAuthorized` and aborts the resolve. The permissive-if-unconfigured rule still applies: a scope with zero policy records always allows, regardless of the enforcement setting.

---

## Concrete example: multi-agent system

Say you have two agents: `agent-prod` (handles production deploys) and `agent-dev` (general development). You want `agent-prod` to have access to production references and `agent-dev` to be blocked from them.

```bash
# Grant agent-prod access to the production project
portunus roles set \
  --scope-type env \
  --scope-value prod \
  --role deployer \
  --actions read \
  --principal agent-prod

# Confirm audit-only mode shows the right behavior before enabling enforcement
portunus audit

# Enable enforcement
portunus roles enforce on
```

After this, `agent-dev` attempting to resolve a `prod` reference will log `would-deny` and — with enforcement on — receive a `NotAuthorized` error. `agent-prod` resolves normally.

---

## Approval gate

Separate from RBAC, individual references can require explicit human approval before any resolve is allowed.

```bash
# Require approval on a reference
portunus gate my-ref

# Grant a time-boxed approval (measured in audit-clock ticks, default 3)
portunus approve my-ref --ttl 5

# Remove the gate
portunus gate my-ref --off
```

A plain `portunus approve` is scoped to the **reference name only**, not to the requesting identity: any caller can resolve the reference until the approval expires. `--once` limits it to a single resolve. Approvals that answer an agent's request (below) are bound to the requester.

### Approval windows

An agent can ask for an approval but cannot grant one.

- **Self-approval is refused.** `portunus approve`, `portunus approval enroll` and `portunus approval remove` exit 1 when an agent marker is set in the environment (`DOSTAL_AGENT`, `PORTUNUS_AGENT`, `AI_AGENT`, `CLAUDECODE`, `CLAUDE_CODE_ENTRYPOINT`, `T3CODE_THREAD_ID`, `MULTICA_AGENT_ID`), and the attempt is audited as `refused:agent requester:<who> marker:<variable>`.
- **The agent asks.** `portunus request-approval <name> --purpose ... --amount ... --merchant ...` (MCP `portunus_request_approval`) records a request, audited as `approval_request requested:<id> requester:<who>`. Requests expire after 15 minutes.
- **The human approves with a factor the agent cannot reach.** `portunus approval enroll` registers a TOTP authenticator app; keep it on your phone. Every approval then needs a current 6-digit code. A code is accepted once, and never an older one after a newer one, so a code seen once cannot be replayed. The human can answer in a T3 Code card, a desktop dialog, or by running `portunus approve <name> --request <id>`.
- **Exactly one resolve.** The approval is single-use: the first resolve claims it with an atomic rename, so of two concurrent resolves only one gets through. It is bound to the requester (`DOSTAL_AGENT`, `PORTUNUS_AGENT` or `MULTICA_AGENT_NAME`, else `agent:$USER`): another caller gets `ApprovalRequired`, audited as `approval-not-for:<who>`, and does not use it up. It must be used within 10 minutes. Non-consuming checks such as leak-scan and sync do not use it up.
- **Audited.** The `approve` line records `approved:<approver> requester:<who> channel:<t3|dialog:<surface>|cli> request:<id> factor:totp uses:1`. Wrong codes are audited as `refused:bad-code`; three wrong codes or a cancelled dialog deny the request.

Without an enrolled factor, `portunus approve` keeps working at a human's terminal but warns, and requests can only be approved there.

**Limits.** The markers and the factor stop an agent from approving by accident or by asking Portunus. They are not an operating-system boundary: a process running as your user with raw access to `~/.portunus` could write an approval file itself. To close that, run agents as a different OS user from the one that owns the vault.

---

## Grant

`portunus grant` records an intended widening of access for a named member **in the audit log only**. It makes no IAM change: nothing is sent to GCP (no `add-iam-policy-binding`) or any other provider, and `roles.json` is not touched. Apply the real permission yourself:

```bash
portunus grant my-ref some-member@example.com
```
