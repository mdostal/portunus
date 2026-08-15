"use client";

import { useEffect, useMemo, useState } from "react";
import type { PortunusPolicy, PortunusReference } from "../types";

const SCOPE_TYPES: PortunusPolicy["scope_type"][] = ["org", "project", "env"];

// Settings (portunus-vault-trust-and-access Slice 7) -- vault-binding
// management stays in Project Explorer (that epic's own deliberate scope
// call, not re-litigated here). This page owns two things new to this
// epic: an org/project hierarchy overview, and role/policy management --
// EDITABLE, writes genuinely persist (unlike the setup wizard's own
// literally-disabled roles step), but always visibly labeled as not yet
// enforced. Never a control that looks live but silently does nothing.
export default function SettingsPage({ refs }: { refs: PortunusReference[] }) {
  const [policies, setPolicies] = useState<Record<string, PortunusPolicy>>({});
  const [policiesError, setPoliciesError] = useState<string | null>(null);
  const [busy, setBusy] = useState(false);
  const [draft, setDraft] = useState({ scope_type: "org" as PortunusPolicy["scope_type"], scope_value: "", role: "", actions: "" });

  function refreshPolicies() {
    fetch("/api/roles")
      .then((r) => r.json())
      .then((data) => setPolicies(data && typeof data === "object" ? data : {}))
      .catch(() => setPolicies({}));
  }

  useEffect(() => {
    refreshPolicies();
  }, []);

  async function submitPolicy(e: React.FormEvent) {
    e.preventDefault();
    if (!draft.scope_value.trim() || !draft.role.trim()) return;
    setBusy(true);
    setPoliciesError(null);
    try {
      const res = await fetch("/api/roles", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ action: "set", ...draft }),
      });
      const data = await res.json();
      if (!res.ok) {
        setPoliciesError(data.error || "failed to set policy");
        return;
      }
      setPolicies(data);
      setDraft({ scope_type: "org", scope_value: "", role: "", actions: "" });
    } finally {
      setBusy(false);
    }
  }

  async function deletePolicy(p: PortunusPolicy) {
    setBusy(true);
    try {
      const res = await fetch("/api/roles", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ action: "delete", scope_type: p.scope_type, scope_value: p.scope_value, role: p.role }),
      });
      if (res.ok) refreshPolicies();
    } finally {
      setBusy(false);
    }
  }

  const orgSummary = useMemo(() => {
    const counts = new Map<string, number>();
    for (const r of refs) {
      const org = r.org || "(no org set)";
      counts.set(org, (counts.get(org) || 0) + 1);
    }
    return [...counts.entries()].sort(([a], [b]) => a.localeCompare(b));
  }, [refs]);

  return (
    <div className="settings-page">
      <section className="settings-section">
        <h2>Organization hierarchy</h2>
        <p className="inline-status">
          {orgSummary.length} org{orgSummary.length === 1 ? "" : "s"} across {refs.length} reference
          {refs.length === 1 ? "" : "s"} -- browse a specific org/project in Vault Map.
        </p>
        <div className="settings-hierarchy-list">
          {orgSummary.map(([org, count]) => (
            <div className="settings-hierarchy-row" key={org}>
              <span>{org}</span>
              <span>{count}</span>
            </div>
          ))}
        </div>
      </section>

      <section className="settings-section settings-stub">
        <h2>Roles &amp; policies</h2>
        <p className="stub-banner">
          ⚠ STUB ONLY -- these records persist for real, but nothing enforces them yet.
          check_injectable()/retag() behave identically whether or not any policy exists here.
          Real access-level enforcement is planned future work (Petitio).
        </p>

        {policiesError && <p className="inline-status error">✗ {policiesError}</p>}

        <div className="settings-hierarchy-list">
          {Object.values(policies).map((p) => (
            <div className="settings-hierarchy-row" key={`${p.scope_type}:${p.scope_value}:${p.role}`}>
              <span>
                {p.scope_type}={p.scope_value} / {p.role}
              </span>
              <span className="policy-actions">{p.actions.join(", ") || "(no actions)"}</span>
              <button className="btn quiet" disabled={busy} onClick={() => deletePolicy(p)}>
                Remove
              </button>
            </div>
          ))}
          {Object.keys(policies).length === 0 && (
            <p className="inline-status">(no policies configured)</p>
          )}
        </div>

        <form className="settings-policy-form" onSubmit={submitPolicy}>
          <select
            className="field"
            value={draft.scope_type}
            disabled={busy}
            onChange={(e) => setDraft((d) => ({ ...d, scope_type: e.target.value as PortunusPolicy["scope_type"] }))}
          >
            {SCOPE_TYPES.map((t) => (
              <option key={t} value={t}>
                {t}
              </option>
            ))}
          </select>
          <input
            className="field"
            placeholder="scope value, e.g. firefly-events"
            value={draft.scope_value}
            disabled={busy}
            onChange={(e) => setDraft((d) => ({ ...d, scope_value: e.target.value }))}
          />
          <input
            className="field"
            placeholder="role, e.g. dev"
            value={draft.role}
            disabled={busy}
            onChange={(e) => setDraft((d) => ({ ...d, role: e.target.value }))}
          />
          <input
            className="field"
            placeholder="actions, e.g. read,test"
            value={draft.actions}
            disabled={busy}
            onChange={(e) => setDraft((d) => ({ ...d, actions: e.target.value }))}
          />
          <button className="btn quiet" type="submit" disabled={busy || !draft.scope_value.trim() || !draft.role.trim()}>
            + add policy
          </button>
        </form>
      </section>
    </div>
  );
}
