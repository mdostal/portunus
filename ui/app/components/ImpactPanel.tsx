"use client";

import { useEffect, useState } from "react";
import type { ImpactNode, ImpactResult, PortunusReference } from "../types";

/** A reference's blast radius (PANT-1122): every reference reachable through
 * `related` -- both directions, transitively -- drawn as a tree, plus the
 * projects/repos/files/injection targets they're wired into. Sits next to
 * the Rotate buttons so a human sees what a rotation touches before they
 * click. Suggested links are shown with their reason and never applied --
 * promoting one is a deliberate edit of `related` via Move…. */
export default function ImpactPanel({
  reference,
  allRefs,
  onSelect,
}: {
  reference: PortunusReference;
  allRefs: PortunusReference[];
  onSelect: (ref: PortunusReference) => void;
}) {
  const [impact, setImpact] = useState<ImpactResult | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [open, setOpen] = useState(false);

  useEffect(() => {
    setImpact(null);
    setError(null);
    fetch(`/api/impact?name=${encodeURIComponent(reference.name)}`)
      .then(async (r) => {
        const data = await r.json();
        if (!r.ok) throw new Error(data.error || "impact failed");
        setImpact(data as ImpactResult);
      })
      .catch((e: Error) => setError(e.message));
    // Re-run when a Move… edit changes the links, not on every re-render.
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [reference.name, (reference.related || []).join(",")]);

  if (error) {
    return <p className="metadata-line impact-error">blast radius unavailable: {error}</p>;
  }
  if (!impact) {
    return <p className="metadata-line">computing blast radius…</p>;
  }

  const s = impact.summary;
  const children: Record<string, ImpactNode[]> = {};
  for (const n of impact.nodes.slice(1)) {
    (children[n.via || ""] ||= []).push(n);
  }

  function renderNode(n: ImpactNode) {
    const target = allRefs.find((r) => r.name === n.name);
    const facts = [
      n.project && `project=${n.project}`,
      n.env && `env=${n.env}`,
      n.repo && `repo=${n.repo}`,
    ].filter(Boolean);
    return (
      <li key={n.name} className={`impact-node impact-${n.direction}`}>
        <div className="impact-node-head">
          {n.direction === "root" ? (
            <strong>{n.name}</strong>
          ) : (
            <button
              type="button"
              className="chip chip-clickable"
              disabled={!target}
              onClick={() => target && onSelect(target)}
              title={n.direction === "related-by" ? `${n.name} lists ${n.via} as related` : `${n.via} lists ${n.name} as related`}
            >
              {n.direction === "related-by" ? "← " : "→ "}
              {n.name}
            </button>
          )}
          {facts.length > 0 && <span className="impact-facts">{facts.join("  ")}</span>}
        </div>
        {(Object.keys(n.injected_as).length > 0 || n.source_files.length > 0) && (
          <div className="impact-wiring">
            {Object.entries(n.injected_as).map(([env, t]) => (
              <span key={`i-${env}`} className="impact-target">{env}={t}</span>
            ))}
            {n.source_files.map((f) => (
              <span key={`f-${f}`} className="impact-file">{n.repo ? `${n.repo}:` : ""}{f}</span>
            ))}
          </div>
        )}
        {(children[n.name] || []).length > 0 && (
          <ul className="impact-children">{children[n.name].map(renderNode)}</ul>
        )}
      </li>
    );
  }

  const touches = s.other_references > 0;
  return (
    <div className={`impact-panel ${touches ? "impact-touches" : ""}`}>
      <button type="button" className="impact-summary" onClick={() => setOpen((v) => !v)}>
        <span className="k">blast radius</span>{" "}
        rotating touches {s.other_references} other reference{s.other_references === 1 ? "" : "s"} in{" "}
        {s.projects.length} project{s.projects.length === 1 ? "" : "s"}
        {s.files.length > 0 && `, ${s.files.length} file${s.files.length === 1 ? "" : "s"}`}
        {" "}{open ? "▾" : "▸"}
      </button>
      {open && (
        <div className="impact-body">
          <ul className="impact-tree">{renderNode(impact.nodes[0])}</ul>
          <p className="metadata-line"><span className="k">projects</span> {s.projects.join(", ") || "-"}</p>
          <p className="metadata-line"><span className="k">repos</span> {s.repos.join(", ") || "-"}</p>
          <p className="metadata-line"><span className="k">envs</span> {s.envs.join(", ") || "-"}</p>
          {impact.unresolved.length > 0 && (
            <p className="metadata-line">
              <span className="k">unresolved</span>{" "}
              {impact.unresolved.map((u) => (
                <span key={`${u.from}-${u.name}`} className="chip chip-unresolved">{u.name} (from {u.from})</span>
              ))}
            </p>
          )}
          {impact.suggested_related.length > 0 && (
            <div className="metadata-line impact-suggested">
              <span className="k">suggested related</span>{" "}
              <span className="impact-hint">not linked, not counted above -- add via Move… → related</span>
              <ul>
                {impact.suggested_related.map((sug) => {
                  const target = allRefs.find((r) => r.name === sug.name);
                  return (
                    <li key={sug.name}>
                      <button
                        type="button"
                        className="chip chip-clickable chip-suggested"
                        disabled={!target}
                        onClick={() => target && onSelect(target)}
                      >
                        {sug.name}
                      </button>{" "}
                      <span className="impact-hint">{sug.reasons.join("; ")}</span>
                    </li>
                  );
                })}
              </ul>
            </div>
          )}
        </div>
      )}
    </div>
  );
}
