"use client";

import { useEffect, useState } from "react";
import type { AuditEntry, LeakFindingDetail, LeakSummary, PortunusReference, PortunusView } from "../types";
import StatePill from "./StatePill";
import RotationBadge from "./RotationBadge";
import CompletenessBadge from "./CompletenessBadge";
import LeakBadge from "./LeakBadge";
import InjectControls from "./InjectControls";

/** {a: "1", b: "2"} -> "a=1,b=2" -- the same k=v,k2=v2 convention tags/
 * injected_as already use everywhere else (AddSecretForm, /api/retag). A
 * local helper, not imported from ui/lib/portunus.ts, since that module
 * pulls in node:child_process and must never load in a client component. */
function dictToKvString(dict: Record<string, string> | undefined): string {
  return Object.entries(dict || {})
    .map(([k, v]) => `${k}=${v}`)
    .join(",");
}

/** WHERE a leak finding came from, in one glance -- a public-repo finding
 * is the single most severity-relevant fact this codebase can surface
 * about a leak (portunus-leak-scan-git-awareness design-discussion.md
 * §5), so it never reads the same as a local log file. */
function findingSourceLabel(f: LeakFindingDetail): { text: string; className: string } {
  if (f.source_kind === "git-history") {
    const repoName = f.repo_path ? f.repo_path.split("/").filter(Boolean).pop() : "repo";
    if (f.repo_visibility === "public") {
      return { text: `⚠ PUBLIC repo: ${repoName}`, className: "finding-source-public" };
    }
    if (f.repo_visibility === "private") {
      return { text: `private repo: ${repoName}`, className: "finding-source-private" };
    }
    return { text: `repo (visibility unknown): ${repoName}`, className: "finding-source-unknown" };
  }
  if (f.source_kind === "log") return { text: "log file", className: "finding-source-log" };
  return { text: "local file", className: "finding-source-local" };
}

export default function DetailDrawer({
  reference,
  allRefs,
  leakSummary,
  onLeakStatusChanged,
  onClose,
  onRotate,
  onMoved,
  onSelectRelated,
}: {
  reference: PortunusReference;
  allRefs: PortunusReference[];
  leakSummary?: LeakSummary;
  onLeakStatusChanged?: () => void;
  onClose: () => void;
  onRotate: (ref: PortunusReference) => void;
  onMoved: () => void;
  onSelectRelated: (ref: PortunusReference) => void;
}) {
  // The compact leakMap the parent passes down only has the aggregate
  // (severity/finding_count) -- the full path/line history (design-
  // discussion.md §5, "show the history") is fetched here, per-reference,
  // only when the drawer is actually open for a leaked reference. Not a
  // per-row fetch anywhere else -- this is the one surface rich enough to
  // justify it.
  const [leakDetail, setLeakDetail] = useState<LeakSummary | null>(null);
  const [markRotatedBusy, setMarkRotatedBusy] = useState(false);

  function refreshLeakDetail() {
    if (!leakSummary?.severity) {
      setLeakDetail(null);
      return;
    }
    fetch(`/api/leak-status?name=${encodeURIComponent(reference.name)}`)
      .then((r) => r.json())
      .then((data: LeakSummary) => setLeakDetail(data))
      .catch(() => setLeakDetail(null));
  }

  useEffect(() => {
    refreshLeakDetail();
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [reference.name, leakSummary?.severity]);

  async function markLeakRotated() {
    setMarkRotatedBusy(true);
    try {
      const res = await fetch("/api/leak-status", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ name: reference.name }),
      });
      if (res.ok) {
        setLeakDetail(null);
        onLeakStatusChanged?.();
      }
    } finally {
      setMarkRotatedBusy(false);
    }
  }
  const [entries, setEntries] = useState<AuditEntry[]>([]);
  const [loading, setLoading] = useState(true);
  const [moveOpen, setMoveOpen] = useState(false);
  const [moveDraft, setMoveDraft] = useState({
    org: reference.org,
    provider: reference.provider,
    project: reference.project,
    env: reference.env,
    description: reference.description,
    purpose: reference.purpose,
    injected_as: dictToKvString(reference.injected_as),
    group: reference.group,
    related: (reference.related || []).join(","),
    repo: reference.repo,
    source_files: (reference.source_files || []).join(","),
  });
  const [moveBusy, setMoveBusy] = useState(false);
  const [moveStatus, setMoveStatus] = useState<string | null>(null);
  const [rotationStatus, setRotationStatus] = useState<{ status: string; account: string } | null>(null);
  // Free-text rotation-account draft, resynced whenever rotationStatus
  // changes (reference switch, or after a successful save) -- same
  // explicit-save pattern as ProjectExplorer's account/wif_audience fields.
  const [rotationAccountDraft, setRotationAccountDraft] = useState("");
  const [rotationAccountBusy, setRotationAccountBusy] = useState(false);
  const [views, setViews] = useState<Record<string, PortunusView>>({});
  const [viewsBusy, setViewsBusy] = useState(false);
  const [viewToAdd, setViewToAdd] = useState("");

  function refreshViews() {
    fetch("/api/views")
      .then((r) => r.json())
      .then((data) => setViews(data && typeof data === "object" ? data : {}))
      .catch(() => setViews({}));
  }

  useEffect(() => {
    refreshViews();
  }, []);

  async function addToView(viewName: string) {
    if (!viewName) return;
    setViewsBusy(true);
    try {
      const res = await fetch("/api/views", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ action: "add", name: viewName, ref_name: reference.name }),
      });
      if (res.ok) refreshViews();
    } finally {
      setViewsBusy(false);
    }
  }

  const [suggestionBusy, setSuggestionBusy] = useState(false);

  // Confirm applies via the SAME /api/retag path a manual edit would use
  // (mirrors the CLI's own `portunus metadata confirm` -- no second write
  // path); reject only clears the sidecar entry, never touches the live
  // field. Both close the drawer + refresh on success -- the same
  // consistency choice submitMove already made for its own mutation.
  async function resolveSuggestion(fieldName: string, action: "confirm" | "reject") {
    setSuggestionBusy(true);
    try {
      const res = await fetch("/api/metadata", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ action, name: reference.name, field: fieldName }),
      });
      if (res.ok) onMoved();
    } finally {
      setSuggestionBusy(false);
    }
  }

  async function removeFromView(viewName: string) {
    setViewsBusy(true);
    try {
      const res = await fetch("/api/views", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ action: "remove", name: viewName, ref_name: reference.name }),
      });
      if (res.ok) refreshViews();
    } finally {
      setViewsBusy(false);
    }
  }

  useEffect(() => {
    let cancelled = false;
    setLoading(true);
    fetch(`/api/audit?secret=${encodeURIComponent(reference.sm_name)}`)
      .then((r) => r.json())
      .then((data: AuditEntry[]) => {
        if (!cancelled) setEntries(data);
      })
      .finally(() => {
        if (!cancelled) setLoading(false);
      });
    return () => {
      cancelled = true;
    };
  }, [reference.sm_name]);

  // Auto-rotate button state is DERIVED from the rotation-provenance
  // registry, not hardcoded -- stays disabled today (every adapter is a
  // stub) but would light up automatically once a real one ships, no UI
  // change needed then. A missing/unconfigured provider is absent from
  // the response entirely, which reads the same as status !== "real".
  useEffect(() => {
    let cancelled = false;
    if (!reference.provider) {
      setRotationStatus(null);
      return;
    }
    fetch(`/api/rotation-status?provider=${encodeURIComponent(reference.provider)}`)
      .then((r) => r.json())
      .then((data: Record<string, { status: string; account: string }>) => {
        if (!cancelled) setRotationStatus(data[reference.provider] || null);
      })
      .catch(() => {
        if (!cancelled) setRotationStatus(null);
      });
    return () => {
      cancelled = true;
    };
  }, [reference.provider]);

  const autoRotateReal = rotationStatus?.status === "real";

  useEffect(() => {
    setRotationAccountDraft(rotationStatus?.account ?? "");
  }, [rotationStatus]);

  // Sets ONLY the account hint -- never status. Mirrors ProjectExplorer's
  // updateBinding() shell-out shape; a stub provider stays a stub no
  // matter what this saves (design-discussion.md §1, research-brief.md §5).
  async function saveRotationAccount() {
    if (!reference.provider) return;
    setRotationAccountBusy(true);
    try {
      const res = await fetch("/api/rotation-status", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ provider: reference.provider, account: rotationAccountDraft }),
      });
      const data = await res.json();
      if (res.ok) {
        setRotationStatus(data[reference.provider] ?? null);
      }
    } finally {
      setRotationAccountBusy(false);
    }
  }

  async function submitMove(e: React.FormEvent) {
    e.preventDefault();
    setMoveBusy(true);
    setMoveStatus(null);
    try {
      const res = await fetch("/api/retag", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ name: reference.name, ...moveDraft }),
      });
      const data = await res.json();
      if (!res.ok) {
        // Surface the CLI's collision error verbatim (names the colliding
        // reference) rather than a generic failure.
        setMoveStatus(`✗ ${data.error}`);
        return;
      }
      setMoveStatus(`✓ ${data.message}`);
      setMoveOpen(false);
      onMoved();
    } catch (err) {
      setMoveStatus(`✗ ${(err as Error).message}`);
    } finally {
      setMoveBusy(false);
    }
  }

  return (
    <aside className="drawer">
      <div className="drawer-head">
        <span className="ref-name">{reference.name}</span>
        <button className="btn quiet" onClick={onClose}>
          Close
        </button>
      </div>
      <StatePill state={reference.state} />
      <RotationBadge reference={reference} prominent />
      <CompletenessBadge reference={reference} prominent />
      <LeakBadge summary={leakSummary} prominent />

      {leakDetail?.severity && (
        <div className="leak-history">
          <p className="stub-banner">
            Detective, not preventive -- this reference's actual value was found in{" "}
            {leakDetail.distinct_files ?? leakDetail.finding_count} conversation
            {(leakDetail.distinct_files ?? leakDetail.finding_count) === 1 ? "" : "s"}. Mark
            rotated only after you've actually rotated the credential yourself -- Portunus can't
            verify that independently.
          </p>
          <div className="settings-hierarchy-list">
            {(leakDetail.findings || []).map((f, i) => {
              const source = findingSourceLabel(f);
              return (
                <div className="settings-hierarchy-row" key={`${f.path}:${f.line_number}:${i}`}>
                  <span title={f.path}>{f.path.split("/").pop()}</span>
                  <span>line {f.line_number}</span>
                  <span className={source.className} title={f.path}>
                    {source.text}
                  </span>
                </div>
              );
            })}
          </div>
          <button className="btn quiet" disabled={markRotatedBusy} onClick={markLeakRotated}>
            {markRotatedBusy ? "Marking…" : "Mark rotated"}
          </button>
        </div>
      )}

      <div className="tags-row">
        {reference.org && <span className="chip">org={reference.org}</span>}
        {reference.provider && <span className="chip">provider={reference.provider}</span>}
        {reference.project && <span className="chip">project={reference.project}</span>}
        {reference.env && <span className="chip">env={reference.env}</span>}
        {reference.repo && <span className="chip">repo={reference.repo}</span>}
        {Object.entries(reference.tags || {}).map(([k, v]) => (
          <span className="chip" key={k}>
            {k}={v}
          </span>
        ))}
      </div>

      {/* Custom views (Slice 4) -- curate task-shaped clustering right
          from the reference you're looking at, the natural place this
          happens ("as I prep them for a project"). */}
      <div className="tags-row">
        {Object.values(views)
          .filter((v) => v.ref_names.includes(reference.name))
          .map((v) => (
            <button
              key={v.name}
              className="chip chip-clickable"
              disabled={viewsBusy}
              onClick={() => removeFromView(v.name)}
              title={`in view "${v.name}" -- click to remove`}
            >
              ✓ {v.name}
            </button>
          ))}
        {Object.values(views).some((v) => !v.ref_names.includes(reference.name)) && (
          <select
            className="field"
            value={viewToAdd}
            disabled={viewsBusy}
            onChange={(e) => {
              addToView(e.target.value);
              setViewToAdd("");
            }}
          >
            <option value="">+ add to view…</option>
            {Object.values(views)
              .filter((v) => !v.ref_names.includes(reference.name))
              .map((v) => (
                <option key={v.name} value={v.name}>
                  {v.name}
                </option>
              ))}
          </select>
        )}
      </div>

      {/* Agent-suggested metadata (Slice 6) -- landed in the suggested{}
          sidecar via portunus_suggest_metadata, NEVER the live field, until
          confirmed here. Reject discards without ever touching the live
          field either. */}
      {Object.entries(reference.suggested || {}).length > 0 && (
        <div className="suggestion-block">
          {Object.entries(reference.suggested || {}).map(([fieldName, info]) => (
            <div className="suggestion-row" key={fieldName}>
              <span className="k">{fieldName}</span>
              <span className="suggestion-value">
                {typeof info.value === "string" ? info.value : JSON.stringify(info.value)}
              </span>
              <span className="suggestion-by">suggested by {info.by}</span>
              <button
                className="btn quiet"
                disabled={suggestionBusy}
                onClick={() => resolveSuggestion(fieldName, "confirm")}
              >
                Confirm
              </button>
              <button
                className="btn quiet"
                disabled={suggestionBusy}
                onClick={() => resolveSuggestion(fieldName, "reject")}
              >
                Reject
              </button>
            </div>
          ))}
        </div>
      )}

      {(reference.description || reference.purpose || Object.keys(reference.injected_as || {}).length > 0
        || reference.group || reference.repo || (reference.related || []).length > 0
        || (reference.source_files || []).length > 0) && (
        <div className="metadata-block">
          {reference.description && (
            <p className="metadata-line">
              <span className="k">description</span> {reference.description}
            </p>
          )}
          {reference.purpose && (
            <p className="metadata-line">
              <span className="k">purpose</span> {reference.purpose}
            </p>
          )}
          {Object.keys(reference.injected_as || {}).length > 0 && (
            <p className="metadata-line">
              <span className="k">injected_as</span>{" "}
              {Object.entries(reference.injected_as).map(([env, target]) => `${env}=${target}`).join(", ")}
            </p>
          )}
          {reference.group && (
            <p className="metadata-line">
              <span className="k">group</span> {reference.group}
            </p>
          )}
          {reference.repo && (
            <p className="metadata-line">
              <span className="k">repo</span> {reference.repo}
            </p>
          )}
          {(reference.source_files || []).length > 0 && (
            <p className="metadata-line">
              <span className="k">source_files</span> {reference.source_files.join(", ")}
            </p>
          )}
          {(reference.related || []).length > 0 && (
            <div className="metadata-line">
              <span className="k">related</span>{" "}
              {reference.related.map((name) => {
                const target = allRefs.find((r) => r.name === name);
                return (
                  <button
                    key={name}
                    type="button"
                    className={`chip chip-clickable ${target ? "" : "chip-unresolved"}`}
                    disabled={!target}
                    onClick={() => target && onSelectRelated(target)}
                    title={target ? `Open ${name}` : `${name} is not in the currently loaded set`}
                  >
                    {name}
                    {!target && " (unresolved)"}
                  </button>
                );
              })}
            </div>
          )}
        </div>
      )}

      <InjectControls reference={reference} />

      {/* Rotate falls back to the add-secret human-entry path rather than a
          hidden server-side regenerate shortcut -- no backend support for
          generating a new value exists yet, and this keeps the same one
          human-plaintext-entry point (Grill U1) instead of adding a second. */}
      <div className="inject-target-row">
        <button className="btn quiet" onClick={() => onRotate(reference)}>
          Rotate…
        </button>
        <button className="btn quiet" onClick={() => setMoveOpen((v) => !v)}>
          Move…
        </button>
        {/* Disabled state is DERIVED from the RotationAdapter registry
            (portunus-metadata-and-rotation-provenance), not hardcoded --
            every provider is a stub today so this stays disabled/inert in
            practice, but it's now real backend state, not a fixed
            attribute. Still no handler even when real: wiring an actual
            click-to-rotate action is future work once a real adapter
            exists to call. */}
        <button
          className="btn quiet"
          disabled={!autoRotateReal}
          title={
            autoRotateReal
              ? `Automated key rotation via ${reference.provider} -- account ${rotationStatus?.account || "-"}`
              : "Automated key rotation -- no real adapter for this provider yet"
          }
        >
          Auto-rotate…
        </button>
      </div>

      {/* Sets ONLY the rotation-account hint (e.g. a Vercel team slug) --
          never `status`, which stays derived from the real adapter
          registry above. This never enables Auto-rotate itself; it only
          makes the tooltip's account hint configurable without the CLI. */}
      {reference.provider && (
        <div className="inject-target-row">
          <label className="form-field">
            <span>Rotation account ({reference.provider})</span>
            <input
              className="field"
              type="text"
              placeholder="free-text context, e.g. a Vercel team slug or GitHub org"
              value={rotationAccountDraft}
              disabled={rotationAccountBusy}
              onChange={(e) => setRotationAccountDraft(e.target.value)}
            />
          </label>
          <button
            type="button"
            className="btn quiet"
            disabled={rotationAccountBusy || rotationAccountDraft === (rotationStatus?.account ?? "")}
            onClick={saveRotationAccount}
          >
            Save
          </button>
        </div>
      )}

      {moveOpen && (
        <form className="inject-controls" onSubmit={submitMove}>
          <div className="form-row">
            <label className="form-field">
              <span>org (umbrella above project)</span>
              <input
                className="field"
                value={moveDraft.org}
                onChange={(e) => setMoveDraft((d) => ({ ...d, org: e.target.value }))}
                placeholder="firefly-events"
              />
            </label>
            <label className="form-field">
              <span>provider</span>
              <input
                className="field"
                value={moveDraft.provider}
                onChange={(e) => setMoveDraft((d) => ({ ...d, provider: e.target.value }))}
              />
            </label>
            <label className="form-field">
              <span>project</span>
              <input
                className="field"
                value={moveDraft.project}
                onChange={(e) => setMoveDraft((d) => ({ ...d, project: e.target.value }))}
              />
            </label>
            <label className="form-field">
              <span>env</span>
              <input
                className="field"
                value={moveDraft.env}
                onChange={(e) => setMoveDraft((d) => ({ ...d, env: e.target.value }))}
              />
            </label>
          </div>
          <div className="form-row">
            <label className="form-field">
              <span>description</span>
              <input
                className="field"
                value={moveDraft.description}
                onChange={(e) => setMoveDraft((d) => ({ ...d, description: e.target.value }))}
              />
            </label>
            <label className="form-field">
              <span>purpose</span>
              <input
                className="field"
                value={moveDraft.purpose}
                onChange={(e) => setMoveDraft((d) => ({ ...d, purpose: e.target.value }))}
              />
            </label>
          </div>
          <label className="form-field">
            <span>injected_as (env=target,env2=target2)</span>
            <input
              className="field"
              value={moveDraft.injected_as}
              onChange={(e) => setMoveDraft((d) => ({ ...d, injected_as: e.target.value }))}
              placeholder="prod=env:STRIPE_KEY,staging=file:.env.staging"
            />
          </label>
          <div className="form-row">
            <label className="form-field">
              <span>group (hierarchical path)</span>
              <input
                className="field"
                value={moveDraft.group}
                onChange={(e) => setMoveDraft((d) => ({ ...d, group: e.target.value }))}
                placeholder="project-y/supabase/auth"
              />
            </label>
            <label className="form-field">
              <span>related (comma-separated reference names)</span>
              <input
                className="field"
                value={moveDraft.related}
                onChange={(e) => setMoveDraft((d) => ({ ...d, related: e.target.value }))}
                placeholder="project-y-mongodb-prod"
              />
            </label>
          </div>
          <div className="form-row">
            <label className="form-field">
              <span>repo (which git repo consumes this)</span>
              <input
                className="field"
                value={moveDraft.repo}
                onChange={(e) => setMoveDraft((d) => ({ ...d, repo: e.target.value }))}
                placeholder="event-api"
              />
            </label>
            <label className="form-field">
              <span>source_files (comma-separated)</span>
              <input
                className="field"
                value={moveDraft.source_files}
                onChange={(e) => setMoveDraft((d) => ({ ...d, source_files: e.target.value }))}
                placeholder="docker-compose.prod.yml"
              />
            </label>
          </div>
          <button className="btn solid" type="submit" disabled={moveBusy}>
            {moveBusy ? "Moving…" : "Save move"}
          </button>
        </form>
      )}
      {moveStatus && <p className="inline-status">{moveStatus}</p>}

      <span className="eyebrow">Audit trail</span>
      {loading && <p className="inline-status">loading…</p>}
      <div className="audit-list">
        {entries.map((e) => (
          <div className="audit-line" key={e.seq}>
            <span className="t">#{e.seq}</span> {e.action} {e.result}
          </div>
        ))}
        {!loading && entries.length === 0 && <p className="inline-status">no entries yet</p>}
      </div>
    </aside>
  );
}
