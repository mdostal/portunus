"use client";

import { useEffect, useState } from "react";
import type { PortunusReference } from "../types";
import StatePill from "./StatePill";
import CompletenessBadge from "./CompletenessBadge";

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

interface VaultBindingInfo {
  backend: string;
  sync_mode: string;
  account: string;
  wif_audience: string;
}

// Two-zone treatment (portunus-swappable-trio, per OSS adapter-marketplace
// research): real, clickable backends vs. honest stubs that never share a
// click target with the real ones. A stub tile opens an explanatory modal
// instead of calling /api/bindings -- for a secrets manager specifically, a
// stub that LOOKS selectable/configured is a safety bug, not a cosmetic gap.
const REAL_BACKENDS = [
  { value: "local", label: "Local" },
  { value: "gcp", label: "GCP" },
];

const STUB_BACKENDS = [
  { value: "aws", label: "AWS Secrets Manager" },
  { value: "vault", label: "HashiCorp Vault" },
  { value: "infisical", label: "Infisical" },
  { value: "doppler", label: "Doppler" },
  { value: "onepassword", label: "1Password" },
  { value: "azure", label: "Azure Key Vault" },
];

function adapterRequestUrl(backendLabel: string): string {
  const title = encodeURIComponent(`[Adapter Request]: ${backendLabel}`);
  return `https://github.com/mdostal/portunus/issues/new?template=adapter-request.yaml&title=${title}`;
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

type TreeFacet = "group" | "repo";

const TREE_FACET_KEY: Record<TreeFacet, (r: PortunusReference) => string> = {
  group: (r) => r.group || "",
  repo: (r) => r.repo || "",
};

const TREE_FACET_BUCKET_LABEL: Record<TreeFacet, string> = {
  group: "(ungrouped)",
  repo: "(no repo set)",
};

/** Same normalization rule as the Python CLI's portunus tree: trim, split
 * on "/", drop empty segments. Two independent implementations (Python,
 * TypeScript), no shared code -- but they must agree on this rule (design-
 * discussion §4 risk). A reference with no value for the active facet is
 * never dropped -- it's the caller's job to bucket it under the facet's
 * own label (ungrouped / no repo set), same as the CLI's --by. */
function buildTree(
  refs: PortunusReference[],
  facet: TreeFacet = "group",
): { ungrouped: PortunusReference[]; root: TreeNode } {
  const keyFn = TREE_FACET_KEY[facet];
  const ungrouped: PortunusReference[] = [];
  const root: TreeNode = { refs: [], children: {} };
  for (const r of refs) {
    const segments = keyFn(r).split("/").map((s) => s.trim()).filter(Boolean);
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
        <CompletenessBadge reference={reference} />
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
  const [treeFacet, setTreeFacet] = useState<TreeFacet>("group");
  const [diff, setDiff] = useState<DiscoverDiff | null>(null);
  const [registerBusy, setRegisterBusy] = useState(false);
  const [conflictNote, setConflictNote] = useState<string | null>(null);
  const [binding, setBinding] = useState<VaultBindingInfo | null>(null);
  const [bindingBusy, setBindingBusy] = useState(false);
  const [stubModal, setStubModal] = useState<{ value: string; label: string } | null>(null);
  // account/wif_audience are free-text, explicit-save fields (design-
  // discussion.md §6) -- drafts track in-progress edits separately from
  // `binding`'s own last-saved values, resynced whenever a fresh binding
  // loads (project switch, or after a successful save).
  const [accountDraft, setAccountDraft] = useState("");
  const [wifAudienceDraft, setWifAudienceDraft] = useState("");

  useEffect(() => {
    setAccountDraft(binding?.account ?? "");
    setWifAudienceDraft(binding?.wif_audience ?? "");
  }, [binding]);

  async function load(p: string) {
    if (!p) return;
    setLoading(true);
    setError(null);
    setConflictNote(null);
    setDiff(null);
    setBinding(null);

    // Independent, not a single try/catch around all three: a project
    // legitimately bound to backend="local" has no live GCP project to
    // discover against at all, so a discover failure there is expected,
    // not fatal -- it must never block registered/bindings from rendering
    // (found live: without this, one 502 from discover hid the new
    // backend/sync_mode controls entirely).
    const [listRes, discoverRes, bindingsRes] = await Promise.allSettled([
      fetch(`/api/list?project=${encodeURIComponent(p)}`).then((r) => r.json().then((d) => ({ ok: r.ok, d }))),
      fetch(`/api/discover?project=${encodeURIComponent(p)}`).then((r) => r.json().then((d) => ({ ok: r.ok, d }))),
      fetch(`/api/bindings?project=${encodeURIComponent(p)}`).then((r) => r.json().then((d) => ({ ok: r.ok, d }))),
    ]);

    if (listRes.status === "fulfilled" && listRes.value.ok) {
      setRegistered(listRes.value.d);
    } else {
      const msg = listRes.status === "fulfilled" ? listRes.value.d.error : (listRes.reason as Error).message;
      setError(msg || "list failed");
    }

    if (discoverRes.status === "fulfilled" && discoverRes.value.ok) {
      setDiff(discoverRes.value.d);
    }
    // discover failing is not surfaced as a page-level error -- expected
    // for local-only/not-yet-GCP-enabled projects.

    if (bindingsRes.status === "fulfilled" && bindingsRes.value.ok) {
      setBinding(bindingsRes.value.d[p] ?? null);
    }

    setLoading(false);
  }

  // Thin shell-out via /api/bindings -- same gating boundary as every other
  // mutation in this UI. Local/GCP route to real backends; AWS is visibly
  // marked not-yet-implemented, matching the CLI's own stub restraint.
  // Accepts a partial update so a single call can carry one field (the
  // backend/sync-mode buttons' click-to-set pattern) or several at once
  // (account + wif_audience, saved together via one explicit button).
  async function updateBinding(fields: Partial<VaultBindingInfo>) {
    setBindingBusy(true);
    try {
      const res = await fetch("/api/bindings", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ project, ...fields }),
      });
      const data = await res.json();
      if (!res.ok) {
        setError(data.error || "bindings set failed");
        return;
      }
      setBinding(data[project] ?? null);
    } catch (err) {
      setError((err as Error).message);
    } finally {
      setBindingBusy(false);
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

      {binding && (
        <>
          <div className="backend-picker">
            <span className="eyebrow">Vault backend</span>
            <div className="backend-zone backend-zone-real">
              {REAL_BACKENDS.map((b) => (
                <button
                  key={b.value}
                  type="button"
                  className={`btn ${binding.backend === b.value ? "solid" : "quiet"}`}
                  disabled={bindingBusy}
                  onClick={() => updateBinding({ backend: b.value })}
                >
                  {b.label}
                </button>
              ))}
            </div>
            <div className="backend-zone backend-zone-stub">
              {STUB_BACKENDS.map((b) => (
                <button
                  key={b.value}
                  type="button"
                  className="btn quiet backend-stub-tile"
                  onClick={() => setStubModal(b)}
                  title={`${b.label} -- not yet implemented`}
                >
                  {b.label}
                </button>
              ))}
            </div>
          </div>
          <label className="form-field">
            <span>Sync mode</span>
            <select
              className="field"
              value={binding.sync_mode}
              disabled={bindingBusy}
              onChange={(e) => updateBinding({ sync_mode: e.target.value })}
            >
              <option value="direct">Direct</option>
              <option value="cached">Cached</option>
            </select>
          </label>
          <div className="backend-picker">
            <span className="eyebrow">Identity</span>
            <label className="form-field">
              <span>Account</span>
              <input
                className="field"
                type="text"
                placeholder="user@example.com -- a local gcloud identity, not a credential"
                value={accountDraft}
                disabled={bindingBusy}
                onChange={(e) => setAccountDraft(e.target.value)}
              />
            </label>
            <label className="form-field">
              <span>WIF audience</span>
              <input
                className="field"
                type="text"
                placeholder="//iam.googleapis.com/projects/.../workloadIdentityPools/.../providers/..."
                value={wifAudienceDraft}
                disabled={bindingBusy}
                onChange={(e) => setWifAudienceDraft(e.target.value)}
              />
            </label>
            <button
              type="button"
              className="btn quiet"
              disabled={
                bindingBusy ||
                (accountDraft === (binding.account ?? "") && wifAudienceDraft === (binding.wif_audience ?? ""))
              }
              onClick={() => updateBinding({ account: accountDraft, wif_audience: wifAudienceDraft })}
            >
              Save identity settings
            </button>
          </div>
        </>
      )}

      {stubModal && (
        <div className="modal-backdrop" onClick={() => setStubModal(null)}>
          <div className="modal" onClick={(e) => e.stopPropagation()}>
            <div className="modal-head">
              <h3>{stubModal.label}</h3>
              <button type="button" className="btn quiet" onClick={() => setStubModal(null)}>
                Close
              </button>
            </div>
            <p className="modal-note">
              This adapter exists in code but does not yet talk to real {stubModal.label}.
              Selecting it will not protect your secret.
            </p>
            <a
              className="btn solid"
              href={adapterRequestUrl(stubModal.label)}
              target="_blank"
              rel="noreferrer"
            >
              Request this adapter on GitHub →
            </a>
          </div>
        </div>
      )}

      {registered.length > 0 && (
        <>
          <div className="explorer-list-head">
            <span className="eyebrow">Registered</span>
            <div className="facet-toggle" role="group" aria-label="Tree facet">
              <button
                type="button"
                className={`btn quiet toggle-btn ${treeFacet === "group" ? "toggle-btn-active" : ""}`}
                onClick={() => setTreeFacet("group")}
              >
                Group
              </button>
              <button
                type="button"
                className={`btn quiet toggle-btn ${treeFacet === "repo" ? "toggle-btn-active" : ""}`}
                onClick={() => setTreeFacet("repo")}
              >
                Repo
              </button>
            </div>
          </div>
          {(() => {
            const { ungrouped, root } = buildTree(registered, treeFacet);
            const presentNames = new Set(registered.map((r) => r.name));
            return (
              <div className="explorer-list">
                {ungrouped.length > 0 && (
                  <div className="tree-branch">
                    <div className="tree-branch-label">{TREE_FACET_BUCKET_LABEL[treeFacet]}</div>
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
