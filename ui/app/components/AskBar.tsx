"use client";

import { useState } from "react";
import type { PortunusReference } from "../types";
import StatePill from "./StatePill";
import InjectControls from "./InjectControls";

export default function AskBar({
  onAdd,
}: {
  onAdd: () => void;
}) {
  const [request, setRequest] = useState("");
  const [busy, setBusy] = useState(false);
  const [resolved, setResolved] = useState<PortunusReference | null>(null);
  const [message, setMessage] = useState<string | null>(null);

  async function ask(e: React.FormEvent) {
    e.preventDefault();
    if (!request.trim()) return;
    setBusy(true);
    setResolved(null);
    setMessage(null);
    try {
      const res = await fetch("/api/ask", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ request }),
      });
      const data = await res.json();
      if (data.resolved) {
        setResolved(data.reference);
      } else {
        setMessage(data.message);
      }
    } catch (err) {
      setMessage((err as Error).message);
    } finally {
      setBusy(false);
    }
  }

  return (
    <aside className="ask-bar">
      <span className="eyebrow">Ask</span>
      <form onSubmit={ask} className="ask-form">
        <input
          className="field mono"
          placeholder='"the vercel secret for mdostal.com in prod"'
          value={request}
          onChange={(e) => setRequest(e.target.value)}
        />
        <button className="btn solid" type="submit" disabled={busy}>
          {busy ? "…" : "Ask"}
        </button>
      </form>

      {resolved && (
        <div className="result-card">
          <div className="result-row-head">
            <span className="ref-name">{resolved.name}</span>
            <StatePill state={resolved.state} />
          </div>
          <div className="tags-row">
            {resolved.provider && <span className="chip">provider={resolved.provider}</span>}
            {resolved.project && <span className="chip">project={resolved.project}</span>}
            {resolved.env && <span className="chip">env={resolved.env}</span>}
          </div>
          <InjectControls reference={resolved} />
        </div>
      )}

      {message && (
        <div className="result-card amb">
          <p className="inline-status">{message}</p>
          {/no match|no reference/i.test(message) && (
            <button className="btn quiet" onClick={onAdd}>
              Add it?
            </button>
          )}
        </div>
      )}

      <p className="ask-hint">
        Never guesses — an ambiguous or unrecognized request asks you to be more specific
        instead of picking one.
      </p>
    </aside>
  );
}
