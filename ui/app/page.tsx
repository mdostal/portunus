"use client";

import { useCallback, useEffect, useState } from "react";
import type { PortunusReference } from "./types";
import Console from "./components/Console";
import VaultMap from "./components/VaultMap";
import AskBar from "./components/AskBar";
import DetailDrawer from "./components/DetailDrawer";
import AddSecretForm from "./components/AddSecretForm";

type Tab = "console" | "map";

export default function Home() {
  const [tab, setTab] = useState<Tab>("console");
  const [askOpen, setAskOpen] = useState(false);
  const [refs, setRefs] = useState<PortunusReference[]>([]);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState<string | null>(null);
  const [selected, setSelected] = useState<PortunusReference | null>(null);
  const [addDraftProvider, setAddDraftProvider] = useState<string | null>(null);
  const [rotateDraft, setRotateDraft] = useState<PortunusReference | null>(null);

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
          {!loading && !error && tab === "console" && <Console refs={refs} onSelect={setSelected} />}
          {!loading && !error && tab === "map" && (
            <VaultMap refs={refs} onSelect={setSelected} onAdd={(provider) => setAddDraftProvider(provider)} />
          )}
        </main>

        {askOpen && <AskBar onAdd={() => setAddDraftProvider("")} />}

        {selected && !addOpen && (
          <DetailDrawer
            reference={selected}
            onClose={() => setSelected(null)}
            onRotate={(ref) => {
              setRotateDraft(ref);
              setSelected(null);
            }}
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
