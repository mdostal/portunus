"use client";

import { Suspense, useCallback, useEffect, useRef, useState } from "react";
import { useSearchParams } from "next/navigation";
import type { AddSecretDraft, LeakSummary, PortunusReference } from "./types";
import Console from "./components/Console";
import VaultMap from "./components/VaultMap";
import AskBar from "./components/AskBar";
import DetailDrawer from "./components/DetailDrawer";
import AddSecretForm from "./components/AddSecretForm";
import ProjectExplorer from "./components/ProjectExplorer";
import SettingsPage from "./components/SettingsPage";
import SetupWizard from "./components/SetupWizard";
import AboutPage from "./components/AboutPage";

type Tab = "console" | "map" | "project" | "settings" | "about";

/** {a: "1", b: "2"} -> "a=1,b=2" -- same convention as DetailDrawer's own
 * local dictToKvString (not imported from there to keep this a plain,
 * dependency-free helper; both independently avoid ui/lib/portunus.ts,
 * which pulls in node:child_process and must never load in a client
 * component). */
function dictToKvString(dict: Record<string, string>): string {
  return Object.entries(dict)
    .map(([k, v]) => `${k}=${v}`)
    .join(",");
}

/** A state=requested reference already carries every metadata field an
 * agent's own `portunus ask "add ..."` knew (portunus-secure-entry Story
 * 03) -- this is a straight field-for-field mapping into AddSecretForm's
 * draft shape, no data invented or guessed. `backend` is deliberately
 * omitted: PortunusReference doesn't expose it today (a separate, larger
 * change to the /api/registry response shape, out of this story's scope) --
 * AddSecretForm's own "(project default)" default applies instead. */
function referenceToDraft(ref: PortunusReference): Partial<AddSecretDraft> {
  return {
    name: ref.name,
    sm_name: ref.sm_name,
    kind: ref.kind,
    scope: ref.scope,
    org: ref.org,
    provider: ref.provider,
    project: ref.project,
    env: ref.env,
    tags: dictToKvString(ref.tags),
    description: ref.description,
    purpose: ref.purpose,
    injected_as: dictToKvString(ref.injected_as),
    group: ref.group,
    related: ref.related.join(","),
    repo: ref.repo,
    source_files: ref.source_files.join(","),
  };
}

export default function Home() {
  return (
    // useSearchParams() (the ?fulfill=<name> deep link, portunus-secure-
    // entry Story 03) opts this page into a Suspense boundary at build/
    // prerender time -- Next.js requires one even though this app never
    // statically prerenders in practice (client-fetched data, output:
    // "standalone"). The fallback is never visible in normal use.
    <Suspense fallback={<div className="shell" />}>
      <HomeInner />
    </Suspense>
  );
}

function HomeInner() {
  const [tab, setTab] = useState<Tab>("console");
  const [askOpen, setAskOpen] = useState(false);
  const [refs, setRefs] = useState<PortunusReference[]>([]);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState<string | null>(null);
  const [selected, setSelected] = useState<PortunusReference | null>(null);
  const [addDraftProvider, setAddDraftProvider] = useState<string | null>(null);
  const [rotateDraft, setRotateDraft] = useState<PortunusReference | null>(null);
  // portunus-secure-entry Story 03: the target of a "Fulfill" action (a
  // one-click path from a state=requested reference into AddSecretForm,
  // fully pre-filled) -- either the DetailDrawer button or a ?fulfill=<name>
  // deep link (Story 04's `portunus ui open --fulfill`).
  const [fulfillDraft, setFulfillDraft] = useState<PortunusReference | null>(null);
  const [fulfillError, setFulfillError] = useState<string | null>(null);
  const searchParams = useSearchParams();
  const fulfillParamHandled = useRef(false);
  // First-run setup wizard (Slice 8) -- null while unknown (avoids a flash
  // of the wizard OR the main app before we actually know), then true/false
  // from `portunus vault status`. Checked once per load; the wizard's own
  // Finish button just flips this to false rather than re-checking, since
  // by then a binding/reference genuinely exists.
  const [needsSetup, setNeedsSetup] = useState<boolean | null>(null);
  // ref_name -> LeakSummary, fetched once per page load (portunus-leak-
  // visibility) and passed down to every surface that renders a
  // reference -- not a per-row fetch, matching CompletenessBadge's own
  // derive-from-already-fetched-data discipline.
  const [leakMap, setLeakMap] = useState<Record<string, LeakSummary>>({});

  useEffect(() => {
    fetch("/api/vault-status")
      .then((r) => r.json())
      .then((data) => setNeedsSetup(data.initialized === false))
      .catch(() => setNeedsSetup(false));
  }, []);

  const refreshLeakMap = useCallback(() => {
    fetch("/api/leak-status")
      .then((r) => r.json())
      .then((data) => {
        const map: Record<string, LeakSummary> = {};
        for (const s of data.statuses || []) map[s.ref_name] = s;
        setLeakMap(map);
      })
      .catch(() => setLeakMap({}));
  }, []);

  useEffect(() => {
    refreshLeakMap();
  }, [refreshLeakMap]);

  const refresh = useCallback(async () => {
    setLoading(true);
    setError(null);
    try {
      const res = await fetch("/api/registry");
      const data = await res.json();
      if (!res.ok) throw new Error(data.error || "failed to load registry");
      setRefs(data);
    } catch (err) {
      setError((err as Error).message);
    } finally {
      setLoading(false);
    }
  }, []);

  useEffect(() => {
    refresh();
  }, [refresh]);

  // Runs once, after the first successful registry load -- never re-fires
  // on a later refresh() (e.g. right after the human successfully submits
  // the fulfill form), which would otherwise misreport a just-fulfilled
  // reference as "no longer pending."
  useEffect(() => {
    if (fulfillParamHandled.current || refs.length === 0) return;
    const name = searchParams.get("fulfill");
    fulfillParamHandled.current = true;
    if (!name) return;
    const ref = refs.find((r) => r.name === name);
    if (!ref) {
      setFulfillError(`No reference named "${name}" was found.`);
      return;
    }
    if (ref.state !== "requested") {
      setFulfillError(`"${name}" is not a pending request (current state: ${ref.state}).`);
      return;
    }
    setFulfillDraft(ref);
  }, [refs, searchParams]);

  const addOpen = addDraftProvider !== null || rotateDraft !== null || fulfillDraft !== null;

  if (needsSetup) {
    return (
      <div className="shell">
        <SetupWizard
          onDone={() => {
            setNeedsSetup(false);
            refresh();
          }}
        />
      </div>
    );
  }

  return (
    <div className="shell">
      <header className="topbar">
        <div className="topbar-title">
          <span className="eyebrow">Portunus</span>
          <h1>Vault</h1>
        </div>
        <nav className="tabs">
          <button className={`tab-btn ${tab === "console" ? "active" : ""}`} onClick={() => setTab("console")}>
            Console
          </button>
          <button className={`tab-btn ${tab === "map" ? "active" : ""}`} onClick={() => setTab("map")}>
            Vault Map
          </button>
          <button className={`tab-btn ${tab === "project" ? "active" : ""}`} onClick={() => setTab("project")}>
            Project Explorer
          </button>
          <button className={`tab-btn ${tab === "settings" ? "active" : ""}`} onClick={() => setTab("settings")}>
            Settings
          </button>
          <button className={`tab-btn ${tab === "about" ? "active" : ""}`} onClick={() => setTab("about")}>
            About
          </button>
        </nav>
        <div className="topbar-actions">
          <button className="btn quiet" onClick={() => setAddDraftProvider("")}>
            + Add secret
          </button>
          <button className={`btn ${askOpen ? "solid" : "quiet"}`} onClick={() => setAskOpen((v) => !v)}>
            Ask
          </button>
        </div>
      </header>

      <div className="layout">
        <main className="main">
          {loading && <p className="inline-status">loading registry…</p>}
          {error && <p className="inline-status error">✗ {error}</p>}
          {fulfillError && (
            <p className="inline-status error">
              ✗ {fulfillError}{" "}
              <button className="btn quiet" onClick={() => setFulfillError(null)}>
                dismiss
              </button>
            </p>
          )}
          {!loading && !error && tab === "console" && (
            <Console refs={refs} onSelect={setSelected} leakMap={leakMap} />
          )}
          {!loading && !error && tab === "map" && (
            <VaultMap
              refs={refs}
              onSelect={setSelected}
              onAdd={(provider) => setAddDraftProvider(provider)}
              leakMap={leakMap}
            />
          )}
          {tab === "project" && <ProjectExplorer onSelect={setSelected} leakMap={leakMap} />}
          {!loading && !error && tab === "settings" && <SettingsPage refs={refs} />}
          {tab === "about" && <AboutPage />}
        </main>

        {askOpen && <AskBar onAdd={() => setAddDraftProvider("")} />}

        {selected && !addOpen && (
          <DetailDrawer
            reference={selected}
            allRefs={refs}
            leakSummary={leakMap[selected.name]}
            onLeakStatusChanged={refreshLeakMap}
            onClose={() => setSelected(null)}
            onRotate={(ref) => {
              setRotateDraft(ref);
              setSelected(null);
            }}
            onFulfill={(ref) => {
              setFulfillDraft(ref);
              setSelected(null);
            }}
            onMoved={() => {
              setSelected(null);
              refresh();
            }}
            onSelectRelated={setSelected}
          />
        )}
      </div>

      {addOpen && (
        <AddSecretForm
          initial={
            rotateDraft
              ? {
                  name: rotateDraft.name,
                  sm_name: rotateDraft.sm_name,
                  provider: rotateDraft.provider,
                  project: rotateDraft.project,
                  env: rotateDraft.env,
                }
              : fulfillDraft
              ? referenceToDraft(fulfillDraft)
              : { provider: addDraftProvider || "" }
          }
          onClose={() => {
            setAddDraftProvider(null);
            setRotateDraft(null);
            setFulfillDraft(null);
          }}
          onAdded={() => {
            setAddDraftProvider(null);
            setRotateDraft(null);
            setFulfillDraft(null);
            refresh();
          }}
        />
      )}
    </div>
  );
}
