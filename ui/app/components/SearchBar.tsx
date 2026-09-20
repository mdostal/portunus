"use client";

import { useState } from "react";
import type { PortunusReference } from "../types";
import StatePill from "./StatePill";

export default function SearchBar() {
  const [query, setQuery] = useState("");
  const [busy, setBusy] = useState(false);
  const [results, setResults] = useState<PortunusReference[] | null>(null);
  const [error, setError] = useState<string | null>(null);

  async function handleSearch(e: React.FormEvent) {
    e.preventDefault();
    const q = query.trim();
    if (!q) return;
    setBusy(true);
    setResults(null);
    setError(null);
    try {
      const params = new URLSearchParams({ query: q });
      const res = await fetch(`/api/search?${params}`);
      const data = await res.json();
      if (!res.ok) throw new Error(data.error || "search failed");
      setResults(data);
    } catch (err) {
      setError((err as Error).message);
    } finally {
      setBusy(false);
    }
  }

  return (
    <section className="search-panel">
      <form onSubmit={handleSearch} className="ask-form">
        <input
          className="field mono"
          placeholder='Search secrets by name, tag, description… (e.g. "linear", "discord")'
          value={query}
          onChange={(e) => setQuery(e.target.value)}
          disabled={busy}
          autoFocus
        />
        <button className="btn solid" type="submit" disabled={busy || !query.trim()}>
          {busy ? "…" : "Search"}
        </button>
      </form>

      {error && <p className="inline-status error">✗ {error}</p>}

      {results !== null && results.length === 0 && (
        <p className="inline-status">No matches for "{query}".</p>
      )}

      {results !== null && results.length > 0 && (
        <ul className="search-results">
          {results.map((ref) => (
            <li key={ref.name} className="result-card">
              <div className="result-row-head">
                <span className="ref-name">{ref.name}</span>
                <StatePill state={ref.state} />
              </div>
              {ref.sm_name && <span className="chip">{ref.sm_name}</span>}
              <div className="tags-row">
                {ref.project && <span className="chip">project={ref.project}</span>}
                {ref.env && <span className="chip">env={ref.env}</span>}
              </div>
            </li>
          ))}
        </ul>
      )}
    </section>
  );
}
