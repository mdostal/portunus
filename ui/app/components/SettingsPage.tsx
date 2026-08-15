"use client";

import { useEffect, useMemo, useState } from "react";
import { checkMetadataCompleteness } from "../completeness";
import type { CrawlCandidate, LeakSummary, PortunusPolicy, PortunusReference } from "../types";

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

  const incompleteRefs = useMemo(
    () => refs.filter((r) => !checkMetadataCompleteness(r).isComplete),
    [refs],
  );

  const [crawlBundle, setCrawlBundle] = useState<CrawlCandidate[] | null>(null);
  const [crawlBusy, setCrawlBusy] = useState(false);
  const [crawlError, setCrawlError] = useState<string | null>(null);
  const [reportBusy, setReportBusy] = useState(false);
  const [reportError, setReportError] = useState<string | null>(null);

  async function fetchCrawlBundle() {
    setCrawlBusy(true);
    setCrawlError(null);
    try {
      const res = await fetch("/api/crawl");
      const data = await res.json();
      if (!res.ok) {
        setCrawlError(data.error || "crawl failed");
        return;
      }
      setCrawlBundle(data.candidates || []);
    } finally {
      setCrawlBusy(false);
    }
  }

  async function copyCrawlBundle() {
    if (!crawlBundle) return;
    await navigator.clipboard.writeText(JSON.stringify(crawlBundle, null, 2));
  }

  async function downloadReport() {
    setReportBusy(true);
    setReportError(null);
    try {
      const res = await fetch("/api/report");
      if (!res.ok) {
        const data = await res.json().catch(() => ({}));
        setReportError(data.error || "report failed");
        return;
      }
      const blob = await res.blob();
      const url = URL.createObjectURL(blob);
      const a = document.createElement("a");
      a.href = url;
      a.download = "portunus-report.md";
      a.click();
      URL.revokeObjectURL(url);
    } finally {
      setReportBusy(false);
    }
  }

  const [scanPaths, setScanPaths] = useState<string[]>([]);
  const [newScanPath, setNewScanPath] = useState("");
  const [leakStatuses, setLeakStatuses] = useState<LeakSummary[]>([]);
  const [leakBusy, setLeakBusy] = useState(false);
  const [leakError, setLeakError] = useState<string | null>(null);
  const [lastScanAt, setLastScanAt] = useState<string | null>(null);

  function refreshScanPaths() {
    fetch("/api/leak-scan-config")
      .then((r) => r.json())
      .then((data) => setScanPaths(data.paths || []))
      .catch(() => setScanPaths([]));
  }

  function refreshLeakStatuses() {
    fetch("/api/leak-status")
      .then((r) => r.json())
      .then((data) => setLeakStatuses(data.statuses || []))
      .catch(() => setLeakStatuses([]));
  }

  useEffect(() => {
    refreshScanPaths();
    refreshLeakStatuses();
  }, []);

  async function addScanPath(e: React.FormEvent) {
    e.preventDefault();
    if (!newScanPath.trim()) return;
    setLeakBusy(true);
    setLeakError(null);
    try {
      const res = await fetch("/api/leak-scan-config", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ action: "add", glob: newScanPath.trim() }),
      });
      const data = await res.json();
      if (!res.ok) {
        setLeakError(data.error || "failed to add scan path");
        return;
      }
      setScanPaths(data.paths || []);
      setNewScanPath("");
    } finally {
      setLeakBusy(false);
    }
  }

  async function removeScanPath(glob: string) {
    setLeakBusy(true);
    try {
      const res = await fetch("/api/leak-scan-config", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ action: "remove", glob }),
      });
      const data = await res.json();
      if (res.ok) setScanPaths(data.paths || []);
    } finally {
      setLeakBusy(false);
    }
  }

  async function runLeakScanNow() {
    setLeakBusy(true);
    setLeakError(null);
    try {
      const res = await fetch("/api/leak-scan", { method: "POST" });
      const data = await res.json();
      if (!res.ok) {
        setLeakError(data.error || "leak-scan failed");
        return;
      }
      setLastScanAt(new Date().toLocaleString());
      refreshLeakStatuses();
    } finally {
      setLeakBusy(false);
    }
  }

  async function markRotated(refName: string) {
    setLeakBusy(true);
    try {
      const res = await fetch("/api/leak-status", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ name: refName }),
      });
      if (res.ok) refreshLeakStatuses();
    } finally {
      setLeakBusy(false);
    }
  }

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

      <section className="settings-section">
        <h2>Crawl &amp; report</h2>
        <p className="inline-status">
          {incompleteRefs.length} reference{incompleteRefs.length === 1 ? "" : "s"} still missing
          description/purpose/org.
        </p>
        <p className="stub-banner">
          The crawl bundle is context for an LLM session (Claude Code, another MCP-connected
          agent, or you) to read and propose metadata from -- it does not fill anything in
          automatically. Nothing here writes to the vault; confirming a suggestion still goes
          through the existing metadata confirm flow.
        </p>

        {crawlError && <p className="inline-status error">✗ {crawlError}</p>}
        {reportError && <p className="inline-status error">✗ {reportError}</p>}

        <div className="settings-actions">
          <button className="btn quiet" disabled={crawlBusy || incompleteRefs.length === 0} onClick={fetchCrawlBundle}>
            {crawlBusy ? "Fetching…" : "Fetch crawl bundle"}
          </button>
          {crawlBundle && crawlBundle.length > 0 && (
            <button className="btn quiet" onClick={copyCrawlBundle}>
              Copy bundle JSON
            </button>
          )}
          <button className="btn quiet" disabled={reportBusy} onClick={downloadReport}>
            {reportBusy ? "Generating…" : "Download report"}
          </button>
        </div>

        {crawlBundle && (
          <div className="settings-hierarchy-list">
            {crawlBundle.length === 0 ? (
              <p className="inline-status">(no candidates -- every matching reference already has description/purpose/org)</p>
            ) : (
              crawlBundle.map((c) => (
                <div className="settings-hierarchy-row" key={c.name}>
                  <span>{c.name}</span>
                  <span>sm_name={c.sm_name}</span>
                  <span>group={c.group || "-"}</span>
                </div>
              ))
            )}
          </div>
        )}
      </section>

      <section className="settings-section">
        <h2>Leak scan</h2>
        <p className="stub-banner">
          Detective, not preventive: this finds secrets that already leaked into the paths you
          configure below -- it does not stop the next paste into a chat window, and it never
          scans anything you haven't explicitly added here. Nothing is auto-rotated;
          "Mark rotated" only records that you rotated the credential yourself.
        </p>

        <p className="inline-status">
          {scanPaths.length} scan path{scanPaths.length === 1 ? "" : "s"} configured
          {lastScanAt ? ` -- last scan ${lastScanAt}` : ""}.
        </p>

        {leakError && <p className="inline-status error">✗ {leakError}</p>}

        <div className="settings-hierarchy-list">
          {scanPaths.map((p) => (
            <div className="settings-hierarchy-row" key={p}>
              <span>{p}</span>
              <button className="btn quiet" disabled={leakBusy} onClick={() => removeScanPath(p)}>
                Remove
              </button>
            </div>
          ))}
          {scanPaths.length === 0 && <p className="inline-status">(no scan paths configured)</p>}
        </div>

        <form className="settings-policy-form" onSubmit={addScanPath}>
          <input
            className="field"
            placeholder="path glob, e.g. ~/.claude/projects/**/*.jsonl"
            value={newScanPath}
            disabled={leakBusy}
            onChange={(e) => setNewScanPath(e.target.value)}
          />
          <button className="btn quiet" type="submit" disabled={leakBusy || !newScanPath.trim()}>
            + add path
          </button>
        </form>

        <div className="settings-actions">
          <button className="btn quiet" disabled={leakBusy || scanPaths.length === 0} onClick={runLeakScanNow}>
            {leakBusy ? "Scanning…" : "Run scan now"}
          </button>
        </div>

        <div className="settings-hierarchy-list">
          {leakStatuses.map((s) => (
            <div className="settings-hierarchy-row" key={s.ref_name}>
              <span>{s.ref_name}</span>
              <span className={`leak-severity leak-severity-${s.severity}`}>{s.severity}</span>
              <span>{s.finding_count} finding{s.finding_count === 1 ? "" : "s"}</span>
              <button className="btn quiet" disabled={leakBusy} onClick={() => markRotated(s.ref_name)}>
                Mark rotated
              </button>
            </div>
          ))}
          {leakStatuses.length === 0 && (
            <p className="inline-status">(no references with active leak findings)</p>
          )}
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
