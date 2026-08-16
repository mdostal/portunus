# Design Discussion — portunus-container-image

## 1. v1 targets same-pod/same-host reachability, not a shared network service

The MCP server being stdio-only today (research-brief.md §2) makes this decision easy rather
than hard: v1 containerizes Portunus as something a CONSUMER reaches locally — via
`docker exec`, a shared pod (Kubernetes sidecar containers in one pod already share a network
namespace and can share a volume), or a harness that spawns `docker run -i ... portunus mcp`
directly as its own MCP client subprocess (this already works today, unchanged, because stdio
transport doesn't care whether the process is a container or a local binary).

This is deliberately NOT "a Portunus service other pods call over the network" — that model
needs real per-caller authentication and the currently-stubbed RBAC (`roles.py`) to actually be
enforced, which is a separate, larger epic (see docs/architecture.md §3, §9's own "stub, not
enforced" precedent — activating it is future work, not silently assumed here). Adding
`sse`/`streamable-http` transport later is a small, well-understood change (the underlying `mcp`
library already supports it) — explicitly flagged as the extension point for that future epic,
not built now.

## 2. Two real v1 use cases, not one abstract "containerize it"

- **CI/build-step sidecar**: `docker run -v portunus-home:/root/.portunus ghcr.io/.../portunus
  resolve --exec ...` — same CLI usage as today, just packaged as an image instead of a local
  pip install. No new capability, just a new distribution form.
- **Kubernetes sidecar container in a pod**: Portunus's container shares a mounted
  `PORTUNUS_HOME` volume (or, for GCP-backend-only use with no local vault, no volume at all)
  with the application container in the same pod; the app reaches Portunus via `kubectl exec`
  equivalent tooling or a shared-volume file-based handoff (e.g. `resolve_to_tempfile`'s existing
  0600-tempfile boundary sink already fits this shape — the app container reads a path the
  sidecar wrote, same invariant as today's local usage, no new sink type needed).

## 3. Persistent-volume requirement — documented as a hard requirement, and checked

An ephemeral (non-mounted) container silently regenerating `master.key` on every restart is a
real, permanent data-loss footgun for the local-encrypted backend specifically (GCP-backend-only
usage has no local vault to lose). Two things address this, not just a docs sentence:

- The Dockerfile declares `PORTUNUS_HOME` as a `VOLUME`, so a container run WITHOUT an explicit
  mount still gets an anonymous volume (survives container restarts, though not
  `docker rm`/`podman rm -v`) rather than silently using ephemeral container-layer storage that
  vanishes on any container removal.
- README/docs are explicit: "if you use the local-encrypted backend, PORTUNUS_HOME MUST be a
  named/bind-mounted volume you control, or every secret becomes permanently unrecoverable the
  next time the container is removed." GCP-backend-only usage doesn't need this warning (no
  local ciphertext to lose) — the docs distinguish the two cases rather than issuing one blanket
  warning that overstates the risk for GCP users.

## 4. Per-backend auth story, documented per environment

- **Local-encrypted backend**: zero external auth needed — works immediately with just a
  mounted volume. The default, simplest path, and the right one to lead with in docs.
- **GCP backend, local Docker/Podman dev**: mount the host's `~/.config/gcloud` directory
  read-only into the container (`-v ~/.config/gcloud:/root/.config/gcloud:ro`) so the container's
  `gcloud` CLI reuses the developer's own already-authenticated ambient identity — no new
  credential, no key file, matches today's local (non-container) `gcloud` ambient-auth behavior
  exactly.
- **GCP backend, real Kubernetes**: GKE Workload Identity (a k8s ServiceAccount annotated to
  impersonate a GCP service account) — keyless, no credential ever touches the container image
  or a mounted volume. This is the RECOMMENDED production path, documented as such, not just
  listed as one option among several.

## 5. Image scope: CLI + MCP server only, not the UI

The Next.js UI/desktop app is a human-facing dashboard, not something you'd run as a k8s
sidecar. v1's image wraps the `portunus` CLI (which also serves as the `portunus mcp` MCP
server entry point) — the actual broker functionality research-brief.md's "what already exists"
section confirms has no UI dependency. A `portunus-ui` image for a hosted team dashboard is a
plausible, clearly-separate future extension, not scoped here.

## 6. `gcloud` CLI baked into one image, not a slim/full split, for v1

Splitting into a slim (local-only) and full (GCP-capable) image is real future polish, but adds
a second image to build/tag/document/keep in sync for a net image-size win that doesn't matter
much for a sidecar container (it isn't shipped to end users' phones — a few hundred extra MB in
a CI/k8s image is a non-issue compared to the maintenance cost of two images this early). v1
ships ONE image with `gcloud` CLI included, so local-only users pay a size cost they don't
strictly need — an explicitly accepted tradeoff, revisit only if it becomes a real complaint.

## 7. Podman compatibility — a genuine gotcha worth documenting, not assuming

Podman's rootless-by-default model remaps container UIDs to unprivileged host UID ranges, which
can make host-bind-mounted volume permissions behave differently than under Docker (files
written by the container's UID inside the volume may not be readable/writable as expected from
the host side, or vice versa, depending on `:z`/`:Z` SELinux labels and subuid mapping). Given
`master.key`/`vault.enc.json`/`registry.json` are all written 0600 by the container's internal
process, this is worth a real, tested note in the docs (`podman run --userns=keep-id` or an
equivalent flag) rather than assuming "Docker-compatible" means byte-identical volume behavior.

## Self-grill

- **Does this conflict with the desktop app's own "supervised service" mode?** No — that's the
  Next.js UI process on one human's machine (§5 already scopes the UI out of this epic's image).
  Genuinely separate concerns, not overlapping work.
- **Should the container run as root or a non-root user?** Non-root, explicitly — `PORTUNUS_HOME`
  file permissions (0600/0700) are already enforced by the application itself regardless of
  container UID; running as root inside the container adds no benefit and is a real, avoidable
  hardening gap for a secret-handling image specifically.
- **What about the WIF-audience/account config that already exists in `vault-bindings.json`?**
  Unchanged — that config lives in `PORTUNUS_HOME`, which is already the mounted-volume story;
  no new config surface needed for containerization itself.

## Scale assessment

Medium: one real Dockerfile with real design care (non-root user, VOLUME declaration, image
scope decision), a `docker-compose.yml` worked example, and a genuinely new docs section
(README + a possible new architecture.md section) covering the persistent-volume requirement
and per-backend/per-environment auth paths. No Python/TypeScript application code changes are
needed — this is packaging and documentation, not new application logic. `version_bump: patch`
(packaging/docs addition, no functional code change to the library itself).
