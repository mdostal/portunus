"use client";

import { useState } from "react";
import type { ApprovalStatus, PortunusReference } from "../types";
import { TTL_CHOICES, approve, describeApproval, setGate, setState } from "../actions";

// The human-only controls the CLI had and the UI did not (GH #257): turn the
// approval gate on/off, approve a gated reference for a time box, and enable
// a dropped value. Each one shells out to the same `portunus` command a
// human would type (gate / approve / state), audited there.
//
// The gate is honour-system against an agent running as the same OS user;
// these buttons make the human path the easy one.
export default function GateControls({
  reference,
  status,
  onChanged,
  compact = false,
}: {
  reference: PortunusReference;
  status?: ApprovalStatus;
  onChanged: () => void;
  compact?: boolean;
}) {
  const [ttl, setTtl] = useState("1h");
  const [busy, setBusy] = useState(false);
  const [msg, setMsg] = useState<string | null>(null);
  const gated = reference.approval === "required";

  async function run(fn: () => Promise<string>) {
    setBusy(true);
    setMsg(null);
    try {
      setMsg(`✓ ${await fn()}`);
      onChanged();
    } catch (err) {
      setMsg(`✗ ${(err as Error).message}`);
    } finally {
      setBusy(false);
    }
  }

  return (
    <section className={`gate-controls ${compact ? "compact" : ""}`} data-testid="gate-controls">
      {!compact && <span className="k">Access</span>}

      {reference.state === "dropped" && (
        <div className="gate-row">
          <span className="gate-note">
            Stored but <strong>not injectable</strong> until enabled.
          </span>
          <button
            className="btn solid"
            disabled={busy}
            onClick={() => run(() => setState(reference.name, "enabled"))}
          >
            Enable
          </button>
        </div>
      )}

      {!compact && (
        <div className="gate-row">
          <span className="gate-note">
            {gated ? "🔒 Approval gate is ON" : "Approval gate is off"}
          </span>
          <button
            className="btn quiet"
            disabled={busy}
            onClick={() => run(() => setGate(reference.name, !gated))}
          >
            {gated ? "Remove gate" : "Require approval"}
          </button>
        </div>
      )}

      {gated && (
        <>
          {status?.approved ? (
            <span className="gate-note gate-approved-text">🔓 {describeApproval(status)}</span>
          ) : status?.pending ? (
            <span className="gate-note gate-pending-text">⏳ An access is waiting on your approval.</span>
          ) : (
            <span className="gate-note">No live approval.</span>
          )}
          <div className="gate-row">
            <select
              className="field"
              aria-label="approval duration"
              value={ttl}
              disabled={busy}
              onChange={(e) => setTtl(e.target.value)}
            >
              {TTL_CHOICES.map((c) => (
                <option key={c.value} value={c.value}>
                  {c.label}
                </option>
              ))}
            </select>
            <button
              className="btn solid"
              disabled={busy}
              onClick={() => run(() => approve(reference.name, ttl))}
            >
              {status?.approved ? "Extend" : "Approve"}
            </button>
          </div>
        </>
      )}

      {msg && <p className="inline-status">{msg}</p>}
    </section>
  );
}
