"use client";

import { useCallback, useEffect, useState } from "react";
import type { LeakSummary, PortunusReference } from "./types";
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

export default function Home() {
  const [tab, setTab] = useState<Tab>("console");
  const [askOpen, setAskOpen] = useState(false);
  const [refs, setRefs] = useState<PortunusReference[]>([]);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState<string | null>(null);
  const [selected, setSelected] = useState<PortunusReference | null>(null);
  const [addDraftProvider, setAddDraftProvider] = useState<string | null>(null);
  const [rotateDraft, setRotateDraft] = useState<PortunusReference | null>(null);
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

  const addOpen = addDraftProvider !== null || rotateDraft !== null;

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
              : { provider: addDraftProvider || "" }
          }
          onClose={() => {
            setAddDraftProvider(null);
            setRotateDraft(null);
          }}
          onAdded={() => {
            setAddDraftProvider(null);
            setRotateDraft(null);
            refresh();
          }}
        />
      )}
    </div>
  );
}
