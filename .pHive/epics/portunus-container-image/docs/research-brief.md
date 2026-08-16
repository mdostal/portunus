# Research Brief — portunus-container-image

## 1. The ask

Raised during OSS-launch cleanup: most real installs won't be "download and run the desktop
app" — they'll be "pull an image and run Portunus alongside another service, as a sidecar
container or in a Kubernetes pod." The user's own framing: local dev happens on Docker (Mac)
and Podman (Arch); "we just need to plan it then build it out."

## 2. What already exists — verified against the real code, not assumed

- **`PORTUNUS_HOME` resolution** (`paths.py`) is already environment-driven —
  `PORTUNUS_HOME -> DOSTAL_SECRETS_HOME -> ~/.portunus`, creates the directory 0700 if missing.
  Nothing about it assumes a real user account or a specific OS; a mounted volume works as-is.
- **`LocalEncryptedBackend`'s master key self-bootstraps** (`localvault.py::_load_or_create_key`)
  — generates a Fernet key and writes it 0600 into `PORTUNUS_HOME` on first use, no human
  passphrase required for normal operation (the `PORTUNUS_PASSPHRASE`-driven `_resolve_passphrase`
  in `cli.py` is ONLY for the export/import archive feature, a separate concern). This means
  containerizing the local backend has no secret-zero bootstrapping problem PROVIDED
  `PORTUNUS_HOME` is a persistent volume — but it also means an ephemeral (non-mounted)
  container silently generates a NEW key on every restart, making every previously-stored value
  permanently undecryptable. This must be documented as a hard requirement, not a footnote.
- **GCP backend shells out to the `gcloud` CLI binary** (`backend.py::GcloudBackend`,
  confirmed via `subprocess`/`shutil.which("gcloud")` calls) — it does NOT use the
  `google-cloud-secret-manager` Python SDK (`pyproject.toml`'s only dependencies are
  `cryptography` and `mcp`). A container image that wants GCP backend support needs the real
  `gcloud` CLI installed, a materially heavier image than the local-only case.
- **Workload Identity Federation (`GCPWorkloadIdentityAuth`, `auth.py`) is keyless already** —
  this is the natural fit for real Kubernetes: GKE Workload Identity binds a k8s ServiceAccount
  to a GCP service account with no key file ever touching the container. This is the production
  auth story to document, not a new capability to build.
- **The MCP server is stdio-only today** (`mcp_server.py::main` calls `mcp.run()` with no
  transport argument). The underlying `mcp` library (`FastMCP.run`) already supports
  `sse`/`streamable-http` transports — Portunus simply never wires past the default. This is a
  real, low-effort future extension point, not a blocker: it means a NETWORK-reachable shared
  Portunus service is possible later without new infrastructure, but it is out of scope for this
  epic's v1 (see design-discussion.md §1 for why v1 deliberately targets same-pod/same-host
  reachability instead).
- **No container support exists today** — confirmed via repo search: no `Dockerfile`, no
  `docker-compose.yml`, no Kubernetes manifests, no container documentation anywhere in the
  repo. This epic is genuinely additive, not fixing/extending an existing partial attempt.
- **The desktop app's own "supervised service" mode** (`docs/architecture.md` §6,
  `GET /api/health`) is the closest existing precedent for "Portunus running unattended,
  supervised by something else" — but it's the Next.js UI process, not the CLI/MCP broker
  itself, and it's designed around one human's one machine, not a container orchestrator.

## 3. Why this needs real design, not just a Dockerfile

Three genuinely non-trivial questions, not mechanical packaging:

1. **Deployment model** — does "container" mean a shared, network-reachable broker service many
   other pods call, or a same-pod/same-host sidecar reached locally? These have very different
   auth/trust implications (the former needs the currently-stubbed RBAC to actually matter; the
   latter inherits today's "one trust boundary" model almost unchanged). The MCP transport
   finding above directly informs this — stdio-only today makes the sidecar model the natural,
   low-risk v1 target.
2. **Persistent-volume requirement for the local backend** — silent, permanent data loss on an
   ephemeral container restart is a real footgun that must be designed around (a startup check?
   at minimum, unmissable documentation) not just mentioned once.
3. **Image scope and auth story per backend** — local-only (self-bootstrapping, no external
   auth) vs. GCP (needs `gcloud` baked in, and a real recommended auth path per environment:
   mounted `~/.config/gcloud` for local Docker/Podman dev, Workload Identity for real k8s).
