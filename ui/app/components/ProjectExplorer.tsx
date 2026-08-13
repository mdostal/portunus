"use client";

import { useState } from "react";
import type { PortunusReference } from "../types";
import StatePill from "./StatePill";

interface DiscoveredSecret {
  sm_name: string;
  labels: Record<string, string>;
  create_time: string;
}

interface DiscoverDiff {
  already_registered: string[];
  not_yet_registered: DiscoveredSecret[];
  wif_configured: boolean;
}

interface DiscoverRegisterResult {
  registered: string[];
  conflicts: string[];
  already_registered: string[];
  wif_configured: boolean;
}

interface TreeNode {
  refs: PortunusReference[];
  children: Record<string, TreeNode>;
}

/** Same normalization rule as the Python CLI's portunus tree: trim, split
 * on "/", drop empty segments. Two independent implementations (Python,
 * TypeScript), no shared code -- but they must agree on this rule (design-
 * discussion §4 risk). A reference with no group is never dropped -- it's
 * the caller's job to bucket it under (ungrouped), same as the CLI. */
function buildTree(refs: PortunusReference[]): { ungrouped: PortunusReference[]; root: TreeNode } {
  const ungrouped: PortunusReference[] = [];
  const root: TreeNode = { refs: [], children: {} };
  for (const r of refs) {
    const segments = (r.group || "").split("/").map((s) => s.trim()).filter(Boolean);
    if (segments.length === 0) {
      ungrouped.push(r);
      continue;
    }
    let node = root;
    for (const seg of segments) {
      if (!node.children[seg]) node.children[seg] = { refs: [], children: {} };
      node = node.children[seg];
    }
    node.refs.push(r);
  }
  return { ungrouped, root };
}

function RelatedChips({ reference, presentNames }: { reference: PortunusReference; presentNames: Set<string> }) {
  if (!reference.related || reference.related.length === 0) return null;
  return (
    <span className="tags-row">
      {reference.related.map((name) => (
        <span className={`chip ${presentNames.has(name) ? "" : "chip-unresolved"}`} key={name}>
          {name}
          {!presentNames.has(name) && " (unresolved)"}
        </span>
      ))}
    </span>
  );
}

function TreeRefRow({
  reference,
  presentNames,
  onSelect,
}: {
  reference: PortunusReference;
  presentNames: Set<string>;
  onSelect: (ref: PortunusReference) => void;
}) {
  return (
    <button className="explorer-row" onClick={() => onSelect(reference)}>
      <span className="ref-name">{reference.name}</span>
      <span>
        {reference.sm_name} <StatePill state={reference.state} />
      </span>
      <RelatedChips reference={reference} presentNames={presentNames} />
    </button>
  );
}

function TreeBranch({
  label,
  node,
  presentNames,
  onSelect,
  depth,
}: {
  label: string;
  node: TreeNode;
  presentNames: Set<string>;
  onSelect: (ref: PortunusReference) => void;
  depth: number;
}) {
  return (
    <div className="tree-branch" style={{ marginLeft: depth === 0 ? 0 : "1rem" }}>
      <div className="tree-branch-label">{label}/</div>
      {node.refs.map((r) => (
        <TreeRefRow reference={r} presentNames={presentNames} onSelect={onSelect} key={r.name} />
      ))}
      {Object.entries(node.children)
        .sort(([a], [b]) => a.localeCompare(b))
        .map(([seg, child]) => (
          <TreeBranch
            label={seg}
            node={child}
            presentNames={presentNames}
            onSelect={onSelect}
            depth={depth + 1}
            key={seg}
          />
        ))}
    </div>
  );
}

export default function ProjectExplorer({ onSelect }: { onSelect: (ref: PortunusReference) => void }) {
  const [project, setProject] = useState("");
  const [loading, setLoading] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [registered, setRegistered] = useState<PortunusReference[]>([]);
  const [diff, setDiff] = useState<DiscoverDiff | null>(null);
  const [registerBusy, setRegisterBusy] = useState(false);
  const [conflictNote, setConflictNote] = useState<string | null>(null);

  async function load(p: string) {
    if (!p) return;
    setLoading(true);
    setError(null);
    setConflictNote(null);
    try {
      const [listRes, discoverRes] = await Promise.all([
        fetch(`/api/list?project=${encodeURIComponent(p)}`),
        fetch(`/api/discover?project=${encodeURIComponent(p)}`),
      ]);
      const listData = await listRes.json();
      if (!listRes.ok) throw new Error(listData.error || "list failed");
      setRegistered(listData);

      const discoverData = await discoverRes.json();
      if (!discoverRes.ok) throw new Error(discoverData.error || "discover failed");
      setDiff(discoverData);
    } catch (err) {
      setError((err as Error).message);
      setDiff(null);
    } finally {
      setLoading(false);
    }
  }

  // `portunus discover --register` has no per-secret selection -- it
  // registers every not-yet-registered secret for the project in one call.
  // This action mirrors that exactly (register-all), never a single-row
  // action, so the UI never implies a scope the CLI can't actually honor.
  async function registerAll() {
    setRegisterBusy(true);
    setConflictNote(null);
    try {
      const res = await fetch("/api/discover", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ project }),
      });
      const data: DiscoverRegisterResult = await res.json();
      if (!res.ok) {
        setError((data as unknown as { error: string }).error || "register failed");
        return;
      }
      if (data.conflicts.length > 0) {
        setConflictNote(
          `${data.conflicts.length} naming conflict(s) skipped -- already point at a different secret: ${data.conflicts.join(", ")}`,
        );
      }
      await load(project);
    } catch (err) {
      setError((err as Error).message);
    } finally {
      setRegisterBusy(false);
    }
  }

  return (
    <div className="project-explorer">
      <form
        className="form-row"
        onSubmit={(e) => {
          e.preventDefault();
          load(project);
        }}
      >
        <label className="form-field">
          <span>GCP project</span>
          <input
            className="field"
            value={project}
            onChange={(e) => setProject(e.target.value)}
            placeholder="personalsites-487021"
          />
        </label>
        <button className="btn solid" type="submit" disabled={loading || !project}>
          {loading ? "Loading…" : "Explore"}
        </button>
      </form>

      {error && <p className="inline-status error">✗ {error}</p>}
      {conflictNote && <p className="inline-status">{conflictNote}</p>}

      {diff && (
        <p className="inline-status">
          GCP WIF: {diff.wif_configured ? "configured" : "not configured"}
        </p>
      )}

      {registered.length > 0 && (
        <>
          <span className="eyebrow">Registered</span>
          {(() => {
            const { ungrouped, root } = buildTree(registered);
            const presentNames = new Set(registered.map((r) => r.name));
            return (
              <div className="explorer-list">
                {ungrouped.length > 0 && (
                  <div className="tree-branch">
                    <div className="tree-branch-label">(ungrouped)</div>
                    {ungrouped.map((r) => (
                      <TreeRefRow reference={r} presentNames={presentNames} onSelect={onSelect} key={r.name} />
                    ))}
                  </div>
                )}
                {Object.entries(root.children)
                  .sort(([a], [b]) => a.localeCompare(b))
                  .map(([seg, child]) => (
                    <TreeBranch
                      label={seg}
                      node={child}
                      presentNames={presentNames}
                      onSelect={onSelect}
                      depth={0}
                      key={seg}
                    />
                  ))}
              </div>
            );
          })()}
        </>
      )}

      {diff && diff.not_yet_registered.length > 0 && (
        <>
          <div className="explorer-section-head">
            <span className="eyebrow">Discoverable in GCP Secret Manager</span>
            <button className="btn quiet" onClick={registerAll} disabled={registerBusy}>
              {registerBusy
                ? "Registering…"
                : `Register all ${diff.not_yet_registered.length}`}
            </button>
          </div>
          {/* One action registers every row below -- portunus discover
              --register has no per-secret selection, so no per-row button
              is shown (it would imply a scope the CLI can't honor). */}
          <div className="explorer-list">
            {diff.not_yet_registered.map((d) => (
              <div className="explorer-row" key={d.sm_name}>
                <span className="ref-name">{d.sm_name}</span>
                <span className="ref-desc">
                  {Object.entries(d.labels).map(([k, v]) => `${k}=${v}`).join(", ")}
                </span>
                <span />
              </div>
            ))}
          </div>
        </>
      )}

      {diff && diff.not_yet_registered.length === 0 && registered.length === 0 && (
        <p className="inline-status">no secrets found for this project</p>
      )}
    </div>
  );
}
