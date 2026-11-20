"use client";

import type { ApprovalStatus, PortunusReference } from "../types";
import StatePill from "./StatePill";
import GateControls from "./GateControls";

// One place for everything waiting on a human (GH #257):
//   - requests: state=requested placeholders an agent asked for -> Fill
//   - approvals: gated references with an access waiting -> Approve
//   - not enabled: dropped values that will not inject yet -> Enable
export default function RequestsView({
  refs,
  approvals,
  onFill,
  onSelect,
  onChanged,
}: {
  refs: PortunusReference[];
  approvals: Record<string, ApprovalStatus>;
  onFill: (ref: PortunusReference) => void;
  onSelect: (ref: PortunusReference) => void;
  onChanged: () => void;
}) {
  const requested = refs.filter((r) => r.state === "requested");
  const pending = refs.filter((r) => r.approval === "required" && approvals[r.name]?.pending);
  const dropped = refs.filter((r) => r.state === "dropped");

  function head(r: PortunusReference) {
    return (
      <div className="request-head">
        <button className="link-btn ref-name" onClick={() => onSelect(r)}>
          {r.name}
        </button>
        <StatePill state={r.state} />
        {r.project && <span className="chip">project={r.project}</span>}
      </div>
    );
  }

  return (
    <div className="requests-view" data-testid="requests-view">
      <section className="request-section">
        <h3>Requested secrets ({requested.length})</h3>
        <p className="modal-note">An agent asked for these. Fill in the value to store it.</p>
        {requested.length === 0 && <p className="inline-status">nothing requested</p>}
        {requested.map((r) => (
          <div className="request-card" key={r.name} data-testid={`request-${r.name}`}>
            {head(r)}
            {(r.purpose || r.description) && <p className="ref-desc-full">{r.purpose || r.description}</p>}
            <button className="btn solid" onClick={() => onFill(r)}>
              Fill…
            </button>
          </div>
        ))}
      </section>

      <section className="request-section">
        <h3>Waiting for approval ({pending.length})</h3>
        <p className="modal-note">Gated secrets something tried to use. Approve for a time box.</p>
        {pending.length === 0 && <p className="inline-status">no pending approvals</p>}
        {pending.map((r) => (
          <div className="request-card" key={r.name} data-testid={`approval-${r.name}`}>
            {head(r)}
            <GateControls reference={r} status={approvals[r.name]} onChanged={onChanged} compact />
          </div>
        ))}
      </section>

      <section className="request-section">
        <h3>Stored, not enabled ({dropped.length})</h3>
        <p className="modal-note">These hold a value but will not inject until enabled.</p>
        {dropped.length === 0 && <p className="inline-status">none</p>}
        {dropped.map((r) => (
          <div className="request-card" key={r.name} data-testid={`dropped-${r.name}`}>
            {head(r)}
            <GateControls reference={r} status={approvals[r.name]} onChanged={onChanged} compact />
          </div>
        ))}
      </section>
    </div>
  );
}
