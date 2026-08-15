"use client";

import { useMemo, useState } from "react";
import type { PortunusReference } from "../types";
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

  const filtered = refs.filter(
    (r) =>
      (!providerFilter || (r.provider || "(none)") === providerFilter) &&
      (!stateFilter || r.state === stateFilter) &&
      matchesCompletenessFilter(r),
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
