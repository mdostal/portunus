"use client";

import { useEffect, useState } from "react";
import type { AuditEntry, PortunusReference } from "../types";
import StatePill from "./StatePill";
import InjectControls from "./InjectControls";

export default function DetailDrawer({
  reference,
  onClose,
  onRotate,
}: {
  reference: PortunusReference;
  onClose: () => void;
  onRotate: (ref: PortunusReference) => void;
}) {
  const [entries, setEntries] = useState<AuditEntry[]>([]);
  const [loading, setLoading] = useState(true);

  useEffect(() => {
    let cancelled = false;
    setLoading(true);
    fetch(`/api/audit?secret=${encodeURIComponent(reference.sm_name)}`)
      .then((r) => r.json())
      .then((data: AuditEntry[]) => {
        if (!cancelled) setEntries(data);
      })
      .finally(() => {
        if (!cancelled) setLoading(false);
      });
    return () => {
      cancelled = true;
    };
  }, [reference.sm_name]);

  return (
    <aside className="drawer">
      <div className="drawer-head">
        <span className="ref-name">{reference.name}</span>
        <button className="btn quiet" onClick={onClose}>
          Close
        </button>
      </div>
      <StatePill state={reference.state} />
      <div className="tags-row">
        {reference.provider && <span className="chip">provider={reference.provider}</span>}
        {reference.project && <span className="chip">project={reference.project}</span>}
        {reference.env && <span className="chip">env={reference.env}</span>}
        {Object.entries(reference.tags || {}).map(([k, v]) => (
          <span className="chip" key={k}>
            {k}={v}
          </span>
        ))}
      </div>

      <InjectControls reference={reference} />

      {/* Rotate falls back to the add-secret human-entry path rather than a
          hidden server-side regenerate shortcut -- no backend support for
          generating a new value exists yet, and this keeps the same one
          human-plaintext-entry point (Grill U1) instead of adding a second. */}
      <button className="btn quiet" onClick={() => onRotate(reference)}>
        Rotate…
      </button>

      <span className="eyebrow">Audit trail</span>
      {loading && <p className="inline-status">loading…</p>}
      <div className="audit-list">
        {entries.map((e) => (
          <div className="audit-line" key={e.seq}>
            <span className="t">#{e.seq}</span> {e.action} {e.result}
          </div>
        ))}
        {!loading && entries.length === 0 && <p className="inline-status">no entries yet</p>}
      </div>
    </aside>
  );
}
