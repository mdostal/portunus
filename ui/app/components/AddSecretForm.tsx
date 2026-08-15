"use client";

import { useState } from "react";
import type { AddSecretDraft } from "../types";

// The deliberate human-plaintext-entry point (Grill U1 resolution). The
// value lives only in this component's local state and the single POST
// body to /api/drop -- never logged, never stored elsewhere in the app,
// cleared from state immediately after a successful submit.
export default function AddSecretForm({
  initial,
  onClose,
  onAdded,
}: {
  initial?: Partial<AddSecretDraft>;
  onClose: () => void;
  onAdded: () => void;
}) {
  const [draft, setDraft] = useState<AddSecretDraft>({
    name: initial?.name || "",
    sm_name: initial?.sm_name || "",
    kind: initial?.kind || "",
    scope: initial?.scope || "",
    backend: initial?.backend || "",
    org: initial?.org || "",
    provider: initial?.provider || "",
    project: initial?.project || "",
    env: initial?.env || "",
    tags: initial?.tags || "",
    description: initial?.description || "",
    purpose: initial?.purpose || "",
    injected_as: initial?.injected_as || "",
    group: initial?.group || "",
    related: initial?.related || "",
    repo: initial?.repo || "",
    source_files: initial?.source_files || "",
  });
  const [value, setValue] = useState("");
  const [status, setStatus] = useState<string | null>(null);
  const [busy, setBusy] = useState(false);

  function field<K extends keyof AddSecretDraft>(key: K) {
    return {
      value: draft[key],
      onChange: (e: React.ChangeEvent<HTMLInputElement>) =>
        setDraft((d) => ({ ...d, [key]: e.target.value })),
    };
  }

  async function submit(e: React.FormEvent) {
    e.preventDefault();
    setBusy(true);
    setStatus(null);
    try {
      const res = await fetch("/api/drop", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ ...draft, value }),
      });
      const data = await res.json();
      if (!res.ok) {
        setStatus(`✗ ${data.error}`);
        return;
      }
      setValue(""); // scrub locally the moment we're done with it
      setStatus(`✓ ${data.message}`);
      onAdded();
    } catch (err) {
      setStatus(`✗ ${(err as Error).message}`);
    } finally {
      setBusy(false);
    }
  }

  return (
    <div className="modal-backdrop" onClick={onClose}>
      <form className="modal" onClick={(e) => e.stopPropagation()} onSubmit={submit}>
        <div className="modal-head">
          <h3>Add secret</h3>
          <button type="button" className="btn quiet" onClick={onClose}>
            Close
          </button>
        </div>
        <p className="modal-note">
          Lands in <code>state=dropped</code> (fail closed) — enable it separately once you've
          confirmed it. The value never leaves this form except to <code>portunus drop</code>.
        </p>
        <label className="form-field">
          <span>name</span>
          <input className="field" required {...field("name")} placeholder="vercel-mdostal" />
        </label>
        <label className="form-field">
          <span>sm_name</span>
          <input className="field" required {...field("sm_name")} placeholder="sm-vercel-mdostal" />
        </label>
        <div className="form-row">
          <label className="form-field">
            <span>kind</span>
            <input className="field" {...field("kind")} placeholder="anthropic" />
          </label>
          <label className="form-field">
            <span>scope</span>
            <input className="field" {...field("scope")} placeholder="shared" />
          </label>
          <label className="form-field">
            <span>backend</span>
            <select
              className="field"
              value={draft.backend}
              onChange={(e) => setDraft((d) => ({ ...d, backend: e.target.value }))}
            >
              <option value="">(project default)</option>
              <option value="local">local</option>
              <option value="gcp">gcp</option>
              <option value="aws">aws</option>
              <option value="vault">vault (stub)</option>
              <option value="infisical">infisical (stub)</option>
              <option value="doppler">doppler (stub)</option>
              <option value="onepassword">onepassword (stub)</option>
              <option value="azure">azure (stub)</option>
            </select>
          </label>
        </div>
        <div className="form-row">
          <label className="form-field">
            <span>org (umbrella above project)</span>
            <input className="field" {...field("org")} placeholder="firefly-events" />
          </label>
          <label className="form-field">
            <span>provider</span>
            <input className="field" {...field("provider")} placeholder="vercel" />
          </label>
          <label className="form-field">
            <span>project</span>
            <input className="field" {...field("project")} placeholder="mdostal.com" />
          </label>
          <label className="form-field">
            <span>env</span>
            <input className="field" {...field("env")} placeholder="prod" />
          </label>
        </div>
        <label className="form-field">
          <span>tags (k=v,k2=v2)</span>
          <input className="field" {...field("tags")} placeholder="team=platform" />
        </label>
        <div className="form-row">
          <label className="form-field">
            <span>description</span>
            <input className="field" {...field("description")} placeholder="what this secret is" />
          </label>
          <label className="form-field">
            <span>purpose</span>
            <input className="field" {...field("purpose")} placeholder="what it's for" />
          </label>
        </div>
        <label className="form-field">
          <span>injected_as (env=target,env2=target2)</span>
          <input className="field" {...field("injected_as")} placeholder="prod=env:STRIPE_KEY" />
        </label>
        <div className="form-row">
          <label className="form-field">
            <span>group (hierarchical path)</span>
            <input className="field" {...field("group")} placeholder="project-y/supabase/auth" />
          </label>
          <label className="form-field">
            <span>related (comma-separated)</span>
            <input className="field" {...field("related")} placeholder="other-ref-name" />
          </label>
        </div>
        <div className="form-row">
          <label className="form-field">
            <span>repo (which git repo consumes this)</span>
            <input className="field" {...field("repo")} placeholder="event-api" />
          </label>
          <label className="form-field">
            <span>source_files (comma-separated)</span>
            <input className="field" {...field("source_files")} placeholder="docker-compose.prod.yml" />
          </label>
        </div>
        <label className="form-field">
          <span>value</span>
          <input
            className="field"
            required
            type="password"
            autoComplete="off"
            value={value}
            onChange={(e) => setValue(e.target.value)}
          />
        </label>
        <button className="btn solid" type="submit" disabled={busy}>
          {busy ? "Dropping…" : "Drop into Arca"}
        </button>
        {status && <p className="inline-status">{status}</p>}
      </form>
    </div>
  );
}
