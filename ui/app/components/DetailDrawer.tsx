"use client";

import { useEffect, useState } from "react";
import type { AuditEntry, PortunusReference } from "../types";
import StatePill from "./StatePill";
import RotationBadge from "./RotationBadge";
import InjectControls from "./InjectControls";

/** {a: "1", b: "2"} -> "a=1,b=2" -- the same k=v,k2=v2 convention tags/
 * injected_as already use everywhere else (AddSecretForm, /api/retag). A
 * local helper, not imported from ui/lib/portunus.ts, since that module
 * pulls in node:child_process and must never load in a client component. */
function dictToKvString(dict: Record<string, string> | undefined): string {
  return Object.entries(dict || {})
    .map(([k, v]) => `${k}=${v}`)
    .join(",");
}

export default function DetailDrawer({
  reference,
  onClose,
  onRotate,
  onMoved,
}: {
  reference: PortunusReference;
  onClose: () => void;
  onRotate: (ref: PortunusReference) => void;
  onMoved: () => void;
}) {
  const [entries, setEntries] = useState<AuditEntry[]>([]);
  const [loading, setLoading] = useState(true);
  const [moveOpen, setMoveOpen] = useState(false);
  const [moveDraft, setMoveDraft] = useState({
    provider: reference.provider,
    project: reference.project,
    env: reference.env,
    description: reference.description,
    purpose: reference.purpose,
    injected_as: dictToKvString(reference.injected_as),
  });
  const [moveBusy, setMoveBusy] = useState(false);
  const [moveStatus, setMoveStatus] = useState<string | null>(null);

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

  async function submitMove(e: React.FormEvent) {
    e.preventDefault();
    setMoveBusy(true);
    setMoveStatus(null);
    try {
      const res = await fetch("/api/retag", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ name: reference.name, ...moveDraft }),
      });
      const data = await res.json();
      if (!res.ok) {
        // Surface the CLI's collision error verbatim (names the colliding
        // reference) rather than a generic failure.
        setMoveStatus(`✗ ${data.error}`);
        return;
      }
      setMoveStatus(`✓ ${data.message}`);
      setMoveOpen(false);
      onMoved();
    } catch (err) {
      setMoveStatus(`✗ ${(err as Error).message}`);
    } finally {
      setMoveBusy(false);
    }
  }

  return (
    <aside className="drawer">
      <div className="drawer-head">
        <span className="ref-name">{reference.name}</span>
        <button className="btn quiet" onClick={onClose}>
          Close
        </button>
      </div>
      <StatePill state={reference.state} />
      <RotationBadge reference={reference} prominent />
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

      {(reference.description || reference.purpose || Object.keys(reference.injected_as || {}).length > 0) && (
        <div className="metadata-block">
          {reference.description && (
            <p className="metadata-line">
              <span className="k">description</span> {reference.description}
            </p>
          )}
          {reference.purpose && (
            <p className="metadata-line">
              <span className="k">purpose</span> {reference.purpose}
            </p>
          )}
          {Object.keys(reference.injected_as || {}).length > 0 && (
            <p className="metadata-line">
              <span className="k">injected_as</span>{" "}
              {Object.entries(reference.injected_as).map(([env, target]) => `${env}=${target}`).join(", ")}
            </p>
          )}
        </div>
      )}

      <InjectControls reference={reference} />

      {/* Rotate falls back to the add-secret human-entry path rather than a
          hidden server-side regenerate shortcut -- no backend support for
          generating a new value exists yet, and this keeps the same one
          human-plaintext-entry point (Grill U1) instead of adding a second. */}
      <div className="inject-target-row">
        <button className="btn quiet" onClick={() => onRotate(reference)}>
          Rotate…
        </button>
        <button className="btn quiet" onClick={() => setMoveOpen((v) => !v)}>
          Move…
        </button>
      </div>

      {moveOpen && (
        <form className="inject-controls" onSubmit={submitMove}>
          <div className="form-row">
            <label className="form-field">
              <span>provider</span>
              <input
                className="field"
                value={moveDraft.provider}
                onChange={(e) => setMoveDraft((d) => ({ ...d, provider: e.target.value }))}
              />
            </label>
            <label className="form-field">
              <span>project</span>
              <input
                className="field"
                value={moveDraft.project}
                onChange={(e) => setMoveDraft((d) => ({ ...d, project: e.target.value }))}
              />
            </label>
            <label className="form-field">
              <span>env</span>
              <input
                className="field"
                value={moveDraft.env}
                onChange={(e) => setMoveDraft((d) => ({ ...d, env: e.target.value }))}
              />
            </label>
          </div>
          <div className="form-row">
            <label className="form-field">
              <span>description</span>
              <input
                className="field"
                value={moveDraft.description}
                onChange={(e) => setMoveDraft((d) => ({ ...d, description: e.target.value }))}
              />
            </label>
            <label className="form-field">
              <span>purpose</span>
              <input
                className="field"
                value={moveDraft.purpose}
                onChange={(e) => setMoveDraft((d) => ({ ...d, purpose: e.target.value }))}
              />
            </label>
          </div>
          <label className="form-field">
            <span>injected_as (env=target,env2=target2)</span>
            <input
              className="field"
              value={moveDraft.injected_as}
              onChange={(e) => setMoveDraft((d) => ({ ...d, injected_as: e.target.value }))}
              placeholder="prod=env:STRIPE_KEY,staging=file:.env.staging"
            />
          </label>
          <button className="btn solid" type="submit" disabled={moveBusy}>
            {moveBusy ? "Moving…" : "Save move"}
          </button>
        </form>
      )}
      {moveStatus && <p className="inline-status">{moveStatus}</p>}

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
