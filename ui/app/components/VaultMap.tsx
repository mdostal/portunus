"use client";

import { useMemo, useState } from "react";
import type { LeakSummary, PortunusReference } from "../types";
import StatePill from "./StatePill";
import RotationBadge from "./RotationBadge";
import CompletenessBadge from "./CompletenessBadge";
import LeakBadge from "./LeakBadge";
import { checkMetadataCompleteness } from "../completeness";

const NO_ORG = "(no org set)";
const NO_PROJECT = "(no project set)";

interface ScopeSummary {
  key: string;
  count: number;
  missing: number;
}

function summarize(refs: PortunusReference[], keyFn: (r: PortunusReference) => string): ScopeSummary[] {
  const byKey = new Map<string, PortunusReference[]>();
  for (const r of refs) {
    const k = keyFn(r);
    if (!byKey.has(k)) byKey.set(k, []);
    byKey.get(k)!.push(r);
  }
  return [...byKey.entries()]
    .map(([key, group]) => ({
      key,
      count: group.length,
      missing: group.filter((r) => !checkMetadataCompleteness(r).isComplete).length,
    }))
    .sort((a, b) => a.key.localeCompare(b.key));
}

// Drill-down navigation (org -> project -> scoped reference list), the fix
// for "the map is a giant flat thing and is unmanageable at 30+ repos"
// (portunus-vault-trust-and-access Slice 3). org/project are the two real
// structural levels the user named directly ("Firefly Events" -> "Shindig")
// -- built entirely on those two fields, no new store, so drilling into a
// scope FEELS like its own small vault (its own reference list, its own
// completeness summary) without being one.
export default function VaultMap({
  refs,
  onSelect,
  onAdd,
  leakMap = {},
}: {
  refs: PortunusReference[];
  onSelect: (ref: PortunusReference) => void;
  onAdd: (provider: string) => void;
  leakMap?: Record<string, LeakSummary>;
}) {
  const [org, setOrg] = useState<string | null>(null);
  const [project, setProject] = useState<string | null>(null);

  const orgSummaries = useMemo(() => summarize(refs, (r) => r.org || NO_ORG), [refs]);

  const refsInOrg = useMemo(
    () => (org === null ? [] : refs.filter((r) => (r.org || NO_ORG) === org)),
    [refs, org],
  );
  const projectSummaries = useMemo(
    () => summarize(refsInOrg, (r) => r.project || NO_PROJECT),
    [refsInOrg],
  );

  const refsInProject = useMemo(
    () => (project === null ? [] : refsInOrg.filter((r) => (r.project || NO_PROJECT) === project)),
    [refsInOrg, project],
  );
  const providerGroups = useMemo(() => {
    const byProvider = new Map<string, PortunusReference[]>();
    for (const r of refsInProject) {
      const p = r.provider || "(untagged)";
      if (!byProvider.has(p)) byProvider.set(p, []);
      byProvider.get(p)!.push(r);
    }
    return [...byProvider.entries()].sort(([a], [b]) => a.localeCompare(b));
  }, [refsInProject]);

  function goToRoot() {
    setOrg(null);
    setProject(null);
  }
  function goToOrg(o: string) {
    setOrg(o);
    setProject(null);
  }

  if (refs.length === 0) {
    return <p className="inline-status">no references yet — add one to get started</p>;
  }

  return (
    <div className="vault-map">
      <nav className="map-breadcrumb">
        <button className="breadcrumb-crumb" onClick={goToRoot} disabled={org === null}>
          All orgs
        </button>
        {org !== null && (
          <>
            <span className="breadcrumb-sep">/</span>
            <button className="breadcrumb-crumb" onClick={() => goToOrg(org)} disabled={project === null}>
              {org}
            </button>
          </>
        )}
        {project !== null && (
          <>
            <span className="breadcrumb-sep">/</span>
            <span className="breadcrumb-crumb current">{project}</span>
          </>
        )}
      </nav>

      {org === null && (
        <div className="scope-cards">
          {orgSummaries.map((s) => (
            <button className="scope-card" key={s.key} onClick={() => goToOrg(s.key)}>
              <span className="scope-card-name">{s.key}</span>
              <span className="scope-card-meta">
                {s.count} reference{s.count === 1 ? "" : "s"}
                {s.missing > 0 && <span className="completeness-badge">⚠ {s.missing} missing</span>}
              </span>
            </button>
          ))}
        </div>
      )}

      {org !== null && project === null && (
        <div className="scope-cards">
          {projectSummaries.map((s) => (
            <button className="scope-card" key={s.key} onClick={() => setProject(s.key)}>
              <span className="scope-card-name">{s.key}</span>
              <span className="scope-card-meta">
                {s.count} reference{s.count === 1 ? "" : "s"}
                {s.missing > 0 && <span className="completeness-badge">⚠ {s.missing} missing</span>}
              </span>
            </button>
          ))}
        </div>
      )}

      {project !== null &&
        providerGroups.map(([providerName, group]) => (
          <div className="gallery-group" key={providerName}>
            <div className="gallery-group-head">{providerName}</div>
            <div className="gallery-cards">
              {group.map((r) => (
                <button className="gallery-card" key={r.name} onClick={() => onSelect(r)}>
                  <span className="ref-name">{r.name}</span>
                  {r.description && <span className="card-desc">{r.description}</span>}
                  <div className="tags-row">
                    {r.env && <span className="chip">{r.env}</span>}
                    <StatePill state={r.state} />
                    <RotationBadge reference={r} />
                    <CompletenessBadge reference={r} />
                    <LeakBadge summary={leakMap[r.name]} />
                  </div>
                </button>
              ))}
              <button
                className="gallery-card add"
                onClick={() => onAdd(providerName === "(untagged)" ? "" : providerName)}
              >
                + add
              </button>
            </div>
          </div>
        ))}
    </div>
  );
}
