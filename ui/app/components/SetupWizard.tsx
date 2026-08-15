"use client";

import { useState } from "react";

type Step = "welcome" | "backend" | "gcp-auth" | "roles" | "discover";

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

// First-run setup wizard (portunus-vault-trust-and-access Slice 8). Only
// ever shown when `portunus vault status` reports an uninitialized
// PORTUNUS_HOME (design-discussion.md §5) -- an already-used vault, however
// empty it looks, never sees this again. Five steps, in the exact order the
// user specified: explain Portunus -> first vault setup (backend choice,
// explicitly framed as "vaults are separate, chosen per level") -> GCP auth
// capture in-UI (only if GCP chosen) -> roles (literally disabled here,
// "coming soon" -- deliberately DIFFERENT treatment from Settings' own
// editable-but-labeled-stub roles section, since a first-run flow is not
// the place to let someone get lost configuring permissions before they
// even have a vault) -> discover/sort, landing in the normal app.
export default function SetupWizard({ onDone }: { onDone: () => void }) {
  const [step, setStep] = useState<Step>("welcome");

  const [projectName, setProjectName] = useState("");
  const [backend, setBackend] = useState("local");
  const [syncMode, setSyncMode] = useState("direct");
  const [bindingBusy, setBindingBusy] = useState(false);
  const [bindingError, setBindingError] = useState<string | null>(null);
  const [stubModal, setStubModal] = useState<{ value: string; label: string } | null>(null);

  const [authEmail, setAuthEmail] = useState("");
  const [authBusy, setAuthBusy] = useState(false);
  const [authStatus, setAuthStatus] = useState<string | null>(null);

  const [discoverBusy, setDiscoverBusy] = useState(false);
  const [discoverResult, setDiscoverResult] = useState<string | null>(null);

  async function saveBindingAndContinue() {
    if (!projectName.trim()) return;
    setBindingBusy(true);
    setBindingError(null);
    try {
      const res = await fetch("/api/bindings", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ project: projectName.trim(), backend, sync_mode: syncMode }),
      });
      const data = await res.json();
      if (!res.ok) {
        setBindingError(data.error || "failed to save binding");
        return;
      }
      setStep(backend === "gcp" ? "gcp-auth" : "roles");
    } finally {
      setBindingBusy(false);
    }
  }

  async function doAuthLogin() {
    if (!authEmail.trim()) return;
    setAuthBusy(true);
    setAuthStatus(null);
    try {
      const res = await fetch("/api/auth-login", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ email: authEmail.trim() }),
      });
      const data = await res.json();
      setAuthStatus(res.ok ? `✓ ${data.message}` : `✗ ${data.error}`);
    } finally {
      setAuthBusy(false);
    }
  }

  async function doDiscover() {
    setDiscoverBusy(true);
    try {
      const res = await fetch("/api/discover", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ project: projectName.trim() }),
      });
      const data = await res.json();
      setDiscoverResult(
        res.ok ? `registered ${data.registered?.length ?? 0} secret(s)` : `✗ ${data.error}`,
      );
    } finally {
      setDiscoverBusy(false);
    }
  }

  return (
    <div className="modal-backdrop">
      <div className="modal wizard-modal">
        {step === "welcome" && (
          <>
            <div className="modal-head">
              <h3>Welcome to Portunus</h3>
            </div>
            <p className="modal-note">
              Portunus is a boundary-only secret manager: a plaintext value never enters an
              LLM/agent turn, a log line, or your shell history. Three parts work together --
            </p>
            <ul className="wizard-explain-list">
              <li>
                <strong>ARCA</strong> -- the vault store itself. Local-encrypted by default;
                optionally GCP Secret Manager (keyless), with other backends as stubs.
              </li>
              <li>
                <strong>OSTIARIUS</strong> -- the engine (CLI/UI/MCP) that resolves a reference
                and injects it at the boundary -- never hands the value back to you or an agent.
              </li>
              <li>
                <strong>Petitio</strong> -- the approval gate. Every access is checked and
                audited; nothing bypasses it.
              </li>
            </ul>
            <button className="btn solid" onClick={() => setStep("backend")}>
              Continue
            </button>
          </>
        )}

        {step === "backend" && (
          <>
            <div className="modal-head">
              <h3>Set up your first vault</h3>
            </div>
            <p className="modal-note">
              A "vault" here is really a project bound to a backend -- this is your first one,
              not your only one. Each project can be separate and different, and you'll choose
              its backend again whenever you add another.
            </p>
            <label className="form-field">
              <span>project name</span>
              <input
                className="field"
                placeholder="e.g. mdostal.com, ffe-cicd, shindig"
                value={projectName}
                disabled={bindingBusy}
                onChange={(e) => setProjectName(e.target.value)}
              />
            </label>
            <div className="backend-picker">
              <span className="eyebrow">Backend</span>
              <div className="backend-zone backend-zone-real">
                {REAL_BACKENDS.map((b) => (
                  <button
                    key={b.value}
                    type="button"
                    className={`btn ${backend === b.value ? "solid" : "quiet"}`}
                    disabled={bindingBusy}
                    onClick={() => setBackend(b.value)}
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
            {backend === "gcp" && (
              <label className="form-field">
                <span>sync mode</span>
                <select className="field" value={syncMode} onChange={(e) => setSyncMode(e.target.value)}>
                  <option value="direct">Direct</option>
                  <option value="cached">Cached</option>
                </select>
              </label>
            )}
            {bindingError && <p className="inline-status error">✗ {bindingError}</p>}
            <button
              className="btn solid"
              disabled={bindingBusy || !projectName.trim()}
              onClick={saveBindingAndContinue}
            >
              {bindingBusy ? "Saving…" : "Continue"}
            </button>
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
                </div>
              </div>
            )}
          </>
        )}

        {step === "gcp-auth" && (
          <>
            <div className="modal-head">
              <h3>Authenticate to GCP</h3>
            </div>
            <p className="modal-note">
              This opens a real browser sign-in window (gcloud's own) -- Portunus doesn't
              replace that step, just gives you a button instead of a terminal command.
            </p>
            <label className="form-field">
              <span>GCP account email</span>
              <input
                className="field"
                placeholder="you@example.com"
                value={authEmail}
                disabled={authBusy}
                onChange={(e) => setAuthEmail(e.target.value)}
              />
            </label>
            <button className="btn quiet" disabled={authBusy || !authEmail.trim()} onClick={doAuthLogin}>
              {authBusy ? "Opening browser…" : "Authenticate"}
            </button>
            {authStatus && <p className="inline-status">{authStatus}</p>}
            <button className="btn solid" onClick={() => setStep("roles")}>
              Continue
            </button>
          </>
        )}

        {step === "roles" && (
          <>
            <div className="modal-head">
              <h3>Roles &amp; permissions</h3>
            </div>
            <p className="stub-banner">
              Coming soon -- who can view, suggest, or edit secrets in this vault will be
              configurable here. Not built yet; nothing below is interactive.
            </p>
            <fieldset disabled className="wizard-roles-preview">
              <label className="form-field">
                <span>role</span>
                <select className="field">
                  <option>owner</option>
                </select>
              </label>
              <button className="btn quiet">+ add policy</button>
            </fieldset>
            <button className="btn solid" onClick={() => setStep("discover")}>
              Continue
            </button>
          </>
        )}

        {step === "discover" && (
          <>
            <div className="modal-head">
              <h3>Find your secrets</h3>
            </div>
            {backend === "gcp" ? (
              <>
                <p className="modal-note">
                  Scan <code>{projectName}</code> for secrets already in GCP Secret Manager and
                  register them (names/labels only -- never a value).
                </p>
                <button className="btn quiet" disabled={discoverBusy} onClick={doDiscover}>
                  {discoverBusy ? "Scanning…" : "Discover secrets"}
                </button>
                {discoverResult && <p className="inline-status">{discoverResult}</p>}
              </>
            ) : (
              <p className="modal-note">
                Your local-encrypted vault is ready. Use "+ Add secret" once you're in, or drop
                one from the CLI (<code>portunus drop</code>).
              </p>
            )}
            <button className="btn solid" onClick={onDone}>
              Finish
            </button>
          </>
        )}
      </div>
    </div>
  );
}
