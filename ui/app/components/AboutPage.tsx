// About/Help (portunus-vault-trust-and-access Slice 9) -- ported from
// README.md's own "Why it's safe"/"Component model" sections, plus new
// content for this epic's own additions. Static content, no fetch -- this
// page documents what exists, it doesn't need to query anything live.
export default function AboutPage() {
  return (
    <div className="about-page">
      <section className="settings-section">
        <h2>What Portunus is</h2>
        <p className="about-p">
          Portunus is a boundary-only secret manager: a plaintext value is fetched only at the
          moment it's injected into a real destination (an env var, a file, an HTTP request) and
          never returned to you, an LLM, or an agent turn. Nothing that can read your chat
          history, logs, or conversation files can ever see a value it fetched through Portunus
          this way -- because it was never handed one to begin with.
        </p>
      </section>

      <section className="settings-section">
        <h2>The three parts</h2>
        <ul className="wizard-explain-list">
          <li>
            <strong>ARCA</strong> -- the vault store. Local-encrypted by default (a harness-local
            key, values never leave your machine); optionally GCP Secret Manager (keyless, via
            Workload Identity Federation); other backends (AWS, Vault, Infisical, Doppler,
            1Password, Azure) exist as honest stubs -- they say so clearly, they never silently
            pretend to protect a value they can't yet reach.
          </li>
          <li>
            <strong>OSTIARIUS</strong> -- the engine: the CLI, the Standalone UI's API routes,
            and the MCP server all shell out to (or call directly into) the SAME resolver. There
            is exactly one implementation of "fetch and inject a value," not three -- so a
            security fix in one surface is a fix everywhere.
          </li>
          <li>
            <strong>Petitio</strong> -- the approval gate. <code>check_injectable()</code> is the
            one chokepoint every resolve/inject path calls before touching a value: a
            dropped/revoked/requested reference fails closed, a gated one needs a time-boxed
            approval. Every decision -- granted or denied -- is written to a tamper-evident audit
            chain.
          </li>
        </ul>
      </section>

      <section className="settings-section">
        <h2>Organizing a large vault</h2>
        <p className="about-p">
          <code>org</code> groups several projects under one umbrella (e.g. "Firefly Events"
          spanning several apps' projects) -- set it when you add a secret, or backfill it later
          via <code>retag</code>/the detail drawer's Move form. Vault Map's org → project
          drill-down uses it directly: pick an org, then a project, and the view scopes to just
          that slice instead of one flat list. <strong>Custom views</strong> (Console's "My
          views" panel) are for task-shaped clustering that doesn't map onto org/project --
          "everything for a specific deploy" -- build one by adding references to it from
          wherever they actually live.
        </p>
      </section>

      <section className="settings-section">
        <h2>Filling in metadata</h2>
        <p className="about-p">
          A reference missing a description, purpose, org, project, or tags gets a "⚠ missing
          metadata" badge -- click Console's Metadata facet to see everything that needs
          attention. An agent can propose a description/purpose/tags/group via the
          <code> portunus_suggest_metadata</code> MCP tool -- it never writes the live field
          directly, only a pending suggestion you'll see in the detail drawer with Confirm/
          Reject buttons. Confirming applies it through the exact same path a manual edit would;
          rejecting discards it without ever touching the reference. Routing fields (org,
          provider, project, env, repo, backend) are never agent-suggestible -- those affect
          which backend a resolve actually uses, so they stay a direct, human-only edit.
        </p>
      </section>

      <section className="settings-section settings-stub">
        <h2>Roles &amp; permissions -- not yet enforced</h2>
        <p className="stub-banner">
          Settings' Roles section lets you record who should be able to do what, at an org/
          project/env scope -- and those records genuinely persist. But nothing reads them yet:
          <code> check_injectable()</code>/<code>retag()</code> behave identically whether or not
          any policy exists. This is deliberate, staged groundwork for real access-level
          enforcement, not a bug. Don't rely on a policy record here to actually restrict
          anything today.
        </p>
      </section>

      <section className="settings-section">
        <h2>Best practices</h2>
        <ul className="wizard-explain-list">
          <li>Fill in <code>description</code>/<code>purpose</code> when you add a secret -- future-you (or a teammate) shouldn't have to guess what it's for.</li>
          <li>Use <code>org</code>/<code>project</code> consistently so Vault Map's drill-down stays meaningful as the vault grows.</li>
          <li>Review agent-suggested metadata before confirming it -- the suggestion is a proposal, not a fact.</li>
          <li>Never paste a secret value into a chat with an agent -- if one is ever exposed that way, rotate it.</li>
          <li>Use <code>portunus vault export</code> for a real backup before a machine move or reinstall.</li>
        </ul>
      </section>

      <section className="settings-section">
        <h2>Getting help</h2>
        <p className="about-p">
          <a href="https://github.com/mdostal/portunus" target="_blank" rel="noreferrer">
            github.com/mdostal/portunus
          </a>{" "}
          has the full README, architecture docs, and issue tracker -- including an adapter-
          request template if you need a stub backend to become real.
        </p>
      </section>
    </div>
  );
}
