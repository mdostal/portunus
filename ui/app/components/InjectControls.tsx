"use client";

import { useState } from "react";
import type { PortunusReference } from "../types";

// The one place a "commit" happens after a reference is already resolved.
// Never touches a value -- posts tags + a target descriptor to /api/inject,
// which shells out to the gated `portunus inject` CLI. Success/failure is
// reported by message only.
export default function InjectControls({ reference }: { reference: PortunusReference }) {
  const [target, setTarget] = useState<"env" | "file">("env");
  const [varName, setVarName] = useState("");
  const [path, setPath] = useState("");
  const [format, setFormat] = useState<"env" | "json" | "yaml">("env");
  const [key, setKey] = useState("");
  const [status, setStatus] = useState<string | null>(null);
  const [busy, setBusy] = useState(false);

  async function inject() {
    setBusy(true);
    setStatus(null);
    try {
      const res = await fetch("/api/inject", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({
          tags: {
            provider: reference.provider,
            project: reference.project,
            env: reference.env,
          },
          target,
          var: varName,
          path,
          format,
          key,
        }),
      });
      const data = await res.json();
      setStatus(res.ok ? `✓ ${data.message}` : `✗ ${data.error}`);
    } catch (err) {
      setStatus(`✗ ${(err as Error).message}`);
    } finally {
      setBusy(false);
    }
  }

  return (
    <div className="inject-controls">
      <div className="inject-target-row">
        <label className="radio">
          <input type="radio" checked={target === "env"} onChange={() => setTarget("env")} />
          env var
        </label>
        <label className="radio">
          <input type="radio" checked={target === "file"} onChange={() => setTarget("file")} />
          file
        </label>
      </div>
      {target === "env" ? (
        <input
          className="field"
          placeholder="VAR_NAME"
          value={varName}
          onChange={(e) => setVarName(e.target.value)}
        />
      ) : (
        <div className="inject-file-row">
          <input className="field" placeholder="/path/to/file" value={path} onChange={(e) => setPath(e.target.value)} />
          <select className="field" value={format} onChange={(e) => setFormat(e.target.value as typeof format)}>
            <option value="env">.env</option>
            <option value="json">json</option>
            <option value="yaml">yaml</option>
          </select>
          <input className="field" placeholder="key" value={key} onChange={(e) => setKey(e.target.value)} />
        </div>
      )}
      <button className="btn solid" onClick={inject} disabled={busy}>
        {busy ? "Injecting…" : "Inject"}
      </button>
      {status && <p className="inline-status">{status}</p>}
    </div>
  );
}
