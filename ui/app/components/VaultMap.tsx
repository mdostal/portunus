"use client";

import { useMemo } from "react";
import type { PortunusReference } from "../types";
import StatePill from "./StatePill";
import RotationBadge from "./RotationBadge";

export default function VaultMap({
  refs,
  onSelect,
  onAdd,
}: {
  refs: PortunusReference[];
  onSelect: (ref: PortunusReference) => void;
  onAdd: (provider: string) => void;
}) {
  const groups = useMemo(() => {
    const byProvider = new Map<string, PortunusReference[]>();
    for (const r of refs) {
      const p = r.provider || "(untagged)";
      if (!byProvider.has(p)) byProvider.set(p, []);
      byProvider.get(p)!.push(r);
    }
    return [...byProvider.entries()].sort(([a], [b]) => a.localeCompare(b));
  }, [refs]);

  return (
    <div className="vault-map">
      {groups.map(([provider, group]) => (
        <div className="gallery-group" key={provider}>
          <div className="gallery-group-head">{provider}</div>
          <div className="gallery-cards">
            {group.map((r) => (
              <button className="gallery-card" key={r.name} onClick={() => onSelect(r)}>
                <span className="ref-name">{r.project || r.name}</span>
                <div className="tags-row">
                  {r.env && <span className="chip">{r.env}</span>}
                  <StatePill state={r.state} />
                  <RotationBadge reference={r} />
                </div>
              </button>
            ))}
            <button className="gallery-card add" onClick={() => onAdd(provider === "(untagged)" ? "" : provider)}>
              + add
            </button>
          </div>
        </div>
      ))}
      {groups.length === 0 && <p className="inline-status">no references yet — add one to get started</p>}
    </div>
  );
}
