"use client";

import { useEffect, useState } from "react";

const DEFAULT_TOKEN_ENDPOINT = "https://oauth2.googleapis.com/token";

type Draft = {
  name: string;
  provider: string;
  account: string;
  org: string;
  project: string;
  env: string;
  tags: string;
  description: string;
  purpose: string;
  injected_as: string;
  group: string;
  related: string;
  repo: string;
  source_files: string;
};

type ManualCredential = {
  client_id: string;
  client_secret: string;
  refresh_token: string;
  token_endpoint: string;
};

// portunus-oauth-token-broker Story 05: the same metadata-rich entry
// pattern AddSecretForm already gives regular secrets, extended to OAuth
// credentials -- with an EXPLICIT distinction between what Portunus can
// auto-fill (a detected local gcloud ADC file, read server-side only,
// never sent to or shown in this browser) and what genuinely needs human
// input (the reference name, provider, account label, and every metadata
// field). Portunus never runs the OAuth consent flow itself -- that stays
// the human's own one-time bootstrap (see README.md's "OAuth token
// broker" section); this form is only the entry surface for the result.
export default function OAuthCredentialForm({
  onClose,
  onAdded,
}: {
  onClose: () => void;
  onAdded: () => void;
}) {
  const [adcAvailable, setAdcAvailable] = useState<boolean | null>(null);
  const [useLocalAdc, setUseLocalAdc] = useState(false);
  const [draft, setDraft] = useState<Draft>({
    name: "",
    provider: "google",
    account: "",
    org: "",
    project: "",
    env: "",
    tags: "",
    description: "",
    purpose: "",
    injected_as: "",
    group: "",
    related: "",
    repo: "",
    source_files: "",
  });
  const [manual, setManual] = useState<ManualCredential>({
    client_id: "",
    client_secret: "",
    refresh_token: "",
    token_endpoint: DEFAULT_TOKEN_ENDPOINT,
  });
  const [status, setStatus] = useState<string | null>(null);
  const [busy, setBusy] = useState(false);

  useEffect(() => {
    fetch("/api/oauth-store?detect=adc")
      .then((r) => r.json())
      .then((data) => setAdcAvailable(Boolean(data?.available)))
      .catch(() => setAdcAvailable(false));
  }, []);

  function field<K extends keyof Draft>(key: K) {
    return {
      value: draft[key],
      onChange: (e: React.ChangeEvent<HTMLInputElement>) =>
        setDraft((d) => ({ ...d, [key]: e.target.value })),
    };
  }

  function manualField<K extends keyof ManualCredential>(key: K) {
    return {
      value: manual[key],
      onChange: (e: React.ChangeEvent<HTMLInputElement>) =>
        setManual((m) => ({ ...m, [key]: e.target.value })),
    };
  }

  async function submit(e: React.FormEvent) {
    e.preventDefault();
    setBusy(true);
    setStatus(null);
    try {
      const body: Record<string, unknown> = { ...draft, use_local_adc: useLocalAdc };
      if (!useLocalAdc) Object.assign(body, manual);
      const res = await fetch("/api/oauth-store", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify(body),
      });
      const data = await res.json();
      if (!res.ok) {
        setStatus(`✗ ${data.error}`);
        return;
      }
      // Scrub locally the moment we're done with it -- same discipline
      // AddSecretForm's own value field already has.
      setManual({ client_id: "", client_secret: "", refresh_token: "", token_endpoint: DEFAULT_TOKEN_ENDPOINT });
      setStatus(`✓ ${data.message}`);
      onAdded();
    } catch (err) {
      setStatus(`✗ ${(err as Error).message}`);
    } finally {
      setBusy(false);
    }
  }

  return (
    <div className="modal-backdrop" onClick={onClose}>
      <form className="modal" onClick={(e) => e.stopPropagation()} onSubmit={submit}>
        <div className="modal-head">
          <h3>Add OAuth credential</h3>
          <button type="button" className="btn quiet" onClick={onClose}>
            Close
          </button>
        </div>
        <p className="modal-note">
          Portunus never runs the OAuth consent flow itself — bootstrap a refresh token yourself
          first (e.g. <code>gcloud auth application-default login --scopes=...</code>), then hand
          the result to Portunus here. Lands in <code>state=dropped</code> (fail closed) — enable
          it separately once you&apos;ve confirmed it.
        </p>

        <label className="form-field">
          <span>reference name</span>
          <input className="field" required {...field("name")} placeholder="my-gmail-token" />
        </label>
        <div className="form-row">
          <label className="form-field">
            <span>provider</span>
            <input className="field" required {...field("provider")} placeholder="google" />
          </label>
          <label className="form-field">
            <span>account label</span>
            <input className="field" required {...field("account")} placeholder="personal" />
          </label>
        </div>

        <div className="form-field">
          <span>credential source</span>
          {adcAvailable === null && (
            <p className="inline-status">checking for a local gcloud ADC file…</p>
          )}
          {adcAvailable === true && (
            <label className="oauth-source-choice">
              <input
                type="checkbox"
                checked={useLocalAdc}
                onChange={(e) => setUseLocalAdc(e.target.checked)}
              />
              <span>
                Auto-fill from local gcloud ADC (
                <code>~/.config/gcloud/application_default_credentials.json</code>) — Portunus
                reads it directly, server-side; the values are never sent to or shown in this
                browser.
              </span>
            </label>
          )}
          {adcAvailable === false && (
            <p className="inline-status">
              No local gcloud ADC file detected — enter the credential manually below.
            </p>
          )}
        </div>

        {!useLocalAdc && (
          <>
            <label className="form-field">
              <span>client_id</span>
              <input className="field" required={!useLocalAdc} {...manualField("client_id")} />
            </label>
            <div className="form-row">
              <label className="form-field">
                <span>client_secret</span>
                <input
                  className="field"
                  type="password"
                  autoComplete="off"
                  required={!useLocalAdc}
                  {...manualField("client_secret")}
                />
              </label>
              <label className="form-field">
                <span>refresh_token</span>
                <input
                  className="field"
                  type="password"
                  autoComplete="off"
                  required={!useLocalAdc}
                  {...manualField("refresh_token")}
                />
              </label>
            </div>
            <label className="form-field">
              <span>token_endpoint</span>
              <input className="field" required={!useLocalAdc} {...manualField("token_endpoint")} />
            </label>
          </>
        )}

        <div className="form-row">
          <label className="form-field">
            <span>org (umbrella above project)</span>
            <input className="field" {...field("org")} placeholder="firefly-events" />
          </label>
          <label className="form-field">
            <span>project</span>
            <input className="field" {...field("project")} placeholder="mdostal.com" />
          </label>
          <label className="form-field">
            <span>env</span>
            <input className="field" {...field("env")} placeholder="prod" />
          </label>
        </div>
        <label className="form-field">
          <span>tags (k=v,k2=v2)</span>
          <input className="field" {...field("tags")} placeholder="team=platform" />
        </label>
        <div className="form-row">
          <label className="form-field">
            <span>description</span>
            <input className="field" {...field("description")} placeholder="what this credential is" />
          </label>
          <label className="form-field">
            <span>purpose</span>
            <input className="field" {...field("purpose")} placeholder="what it's for" />
          </label>
        </div>
        <label className="form-field">
          <span>injected_as (env=target,env2=target2)</span>
          <input className="field" {...field("injected_as")} placeholder="prod=env:GMAIL_TOKEN" />
        </label>
        <div className="form-row">
          <label className="form-field">
            <span>group (hierarchical path)</span>
            <input className="field" {...field("group")} placeholder="project-y/gmail" />
          </label>
          <label className="form-field">
            <span>related (comma-separated)</span>
            <input className="field" {...field("related")} placeholder="other-ref-name" />
          </label>
        </div>
        <div className="form-row">
          <label className="form-field">
            <span>repo (which git repo consumes this)</span>
            <input className="field" {...field("repo")} placeholder="event-api" />
          </label>
          <label className="form-field">
            <span>source_files (comma-separated)</span>
            <input className="field" {...field("source_files")} placeholder="docker-compose.prod.yml" />
          </label>
        </div>

        <button className="btn solid" type="submit" disabled={busy}>
          {busy ? "Storing…" : "Store & register"}
        </button>
        {status && <p className="inline-status">{status}</p>}
      </form>
    </div>
  );
}
