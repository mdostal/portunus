"use client";

import { useEffect, useMemo, useState } from "react";
import type { PortunusReference, PortunusView } from "../types";
import StatePill from "./StatePill";
import RotationBadge from "./RotationBadge";
import CompletenessBadge from "./CompletenessBadge";
import { checkMetadataCompleteness } from "../completeness";

export default function Console({
  refs,
  onSelect,
}: {
  refs: PortunusReference[];
  onSelect: (ref: PortunusReference) => void;
}) {
  const [providerFilter, setProviderFilter] = useState<string | null>(null);
  const [stateFilter, setStateFilter] = useState<string | null>(null);
  const [completenessFilter, setCompletenessFilter] = useState<"complete" | "missing" | null>(null);

  // Custom views (portunus-vault-trust-and-access Slice 4) -- task-shaped
  // clustering ("everything for the Shindig deploy"), orthogonal to the
  // structural provider/state/metadata facets above. Fetched once here
  // (not per-row) since a view filter applies to the whole list, same as
  // every other facet.
  const [views, setViews] = useState<Record<string, PortunusView>>({});
  const [viewFilter, setViewFilter] = useState<string | null>(null);
  const [newViewName, setNewViewName] = useState("");
  const [viewsBusy, setViewsBusy] = useState(false);

  function refreshViews() {
    fetch("/api/views")
      .then((r) => r.json())
      .then((data) => setViews(data && typeof data === "object" ? data : {}))
      .catch(() => setViews({}));
  }

  useEffect(() => {
    refreshViews();
  }, []);

  async function createView(e: React.FormEvent) {
    e.preventDefault();
    const name = newViewName.trim();
    if (!name) return;
    setViewsBusy(true);
    try {
      const res = await fetch("/api/views", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ action: "create", name }),
      });
      if (res.ok) {
        setNewViewName("");
        refreshViews();
      }
    } finally {
      setViewsBusy(false);
    }
  }

  const providers = useMemo(() => {
    const counts = new Map<string, number>();
    for (const r of refs) {
      const p = r.provider || "(none)";
      counts.set(p, (counts.get(p) || 0) + 1);
    }
    return [...counts.entries()];
  }, [refs]);

  const states = useMemo(() => {
    const counts = new Map<string, number>();
    for (const r of refs) counts.set(r.state, (counts.get(r.state) || 0) + 1);
    return [...counts.entries()];
  }, [refs]);

  // Sorting/facet answer to "a warning if we don't have extra metadata and
  // some project tags" -- a real, clickable filter, not just a badge you
  // have to scan the whole list to notice.
  const completenessCounts = useMemo(() => {
    let missing = 0;
    for (const r of refs) if (!checkMetadataCompleteness(r).isComplete) missing += 1;
    return { missing, complete: refs.length - missing };
  }, [refs]);

  function matchesCompletenessFilter(r: PortunusReference): boolean {
    if (!completenessFilter) return true;
    const isComplete = checkMetadataCompleteness(r).isComplete;
    return completenessFilter === "complete" ? isComplete : !isComplete;
  }

  const activeViewRefNames = viewFilter ? new Set(views[viewFilter]?.ref_names || []) : null;

  const filtered = refs.filter(
    (r) =>
      (!providerFilter || (r.provider || "(none)") === providerFilter) &&
      (!stateFilter || r.state === stateFilter) &&
      matchesCompletenessFilter(r) &&
      (!activeViewRefNames || activeViewRefNames.has(r.name)),
  );

  return (
    <div className="console">
      <aside className="console-rail">
        <div className="rail-group">
          <span className="k">Provider</span>
          {providers.map(([p, n]) => (
            <button
              key={p}
              className={`rail-facet ${providerFilter === p ? "active" : ""}`}
              onClick={() => setProviderFilter(providerFilter === p ? null : p)}
            >
              <span>{p}</span>
              <span>{n}</span>
            </button>
          ))}
        </div>
        <div className="rail-group">
          <span className="k">State</span>
          {states.map(([s, n]) => (
            <button
              key={s}
              className={`rail-facet ${stateFilter === s ? "active" : ""}`}
              onClick={() => setStateFilter(stateFilter === s ? null : s)}
            >
              <span>{s}</span>
              <span>{n}</span>
            </button>
          ))}
        </div>
        {completenessCounts.missing > 0 && (
          <div className="rail-group">
            <span className="k">Metadata</span>
            <button
              className={`rail-facet ${completenessFilter === "missing" ? "active" : ""}`}
              onClick={() => setCompletenessFilter(completenessFilter === "missing" ? null : "missing")}
            >
              <span>⚠ missing</span>
              <span>{completenessCounts.missing}</span>
            </button>
            <button
              className={`rail-facet ${completenessFilter === "complete" ? "active" : ""}`}
              onClick={() => setCompletenessFilter(completenessFilter === "complete" ? null : "complete")}
            >
              <span>complete</span>
              <span>{completenessCounts.complete}</span>
            </button>
          </div>
        )}
        <div className="rail-group">
          <span className="k">My views</span>
          {Object.values(views).map((v) => (
            <button
              key={v.name}
              className={`rail-facet ${viewFilter === v.name ? "active" : ""}`}
              onClick={() => setViewFilter(viewFilter === v.name ? null : v.name)}
              title={v.description}
            >
              <span>{v.name}</span>
              <span>{v.ref_names.length}</span>
            </button>
          ))}
          <form className="view-create-form" onSubmit={createView}>
            <input
              className="field"
              placeholder="new view name"
              value={newViewName}
              disabled={viewsBusy}
              onChange={(e) => setNewViewName(e.target.value)}
            />
            <button className="btn quiet" type="submit" disabled={viewsBusy || !newViewName.trim()}>
              + create
            </button>
          </form>
        </div>
      </aside>

      <div className="console-main">
        <div className="console-table-head">
          <span>reference</span>
          <span>provider</span>
          <span>project</span>
          <span>env</span>
          <span>state</span>
        </div>
        {filtered.map((r) => (
          <button className="console-table-row" key={r.name} onClick={() => onSelect(r)}>
            <span className="ref-cell">
              <span className="ref-name">{r.name}</span>
              {r.description && <span className="ref-desc">{r.description}</span>}
            </span>
            <span>{r.provider || "—"}</span>
            <span>{r.project || "—"}</span>
            <span>{r.env || "—"}</span>
            <span>
              <StatePill state={r.state} />
              <RotationBadge reference={r} />
              <CompletenessBadge reference={r} />
            </span>
          </button>
        ))}
        {filtered.length === 0 && <p className="inline-status">no references match these filters</p>}
      </div>
    </div>
  );
}
