# Portunus (CLI + MCP server) as a container -- the sidecar/CI/Kubernetes
# distribution form. See .pHive/epics/portunus-container-image/docs/ for
# the design reasoning: this image targets same-pod/same-host reachability
# (docker exec, a shared pod volume/network namespace, or a harness
# spawning `docker run -i ... portunus mcp` directly), not a
# network-reachable shared broker service.
#
# One image with the gcloud CLI included (not a slim/local-only split) --
# a deliberate v1 tradeoff (design-discussion.md §6): image size doesn't
# matter much for a sidecar/CI image, and two images is real ongoing
# maintenance cost for an early, unproven feature.
FROM python:3.12-slim

# gcloud CLI -- required by GcloudBackend, which shells out to the real
# `gcloud` binary (not a Python SDK -- see backend.py). Installed via
# Google's own documented apt repo, not a third-party script.
RUN apt-get update \
    && apt-get install -y --no-install-recommends curl gnupg ca-certificates \
    && echo "deb [signed-by=/usr/share/keyrings/cloud.google.gpg] https://packages.cloud.google.com/apt cloud-sdk main" \
       > /etc/apt/sources.list.d/google-cloud-sdk.list \
    && curl -s https://packages.cloud.google.com/apt/doc/apt-key.gpg \
       | gpg --dearmor -o /usr/share/keyrings/cloud.google.gpg \
    && apt-get update \
    && apt-get install -y --no-install-recommends google-cloud-cli \
    && apt-get remove -y gnupg curl \
    && apt-get autoremove -y \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /build
COPY pyproject.toml README.md ./
COPY src/ src/
RUN pip install --no-cache-dir .

# Non-root -- PORTUNUS_HOME's own file permissions (0600/0700) are already
# the real access control; running as root inside the container adds no
# benefit and is an avoidable hardening gap for a secret-handling image
# (design-discussion.md self-grill).
RUN useradd --create-home --uid 10001 portunus

# PORTUNUS_HOME as a declared VOLUME -- a container run WITHOUT an
# explicit mount still gets a persistent anonymous volume (survives
# restarts) instead of silently ephemeral container-layer storage.
# design-discussion.md §3: for the local-encrypted backend specifically,
# an unmounted/removed volume means every stored secret becomes
# permanently unrecoverable -- always bind-mount a real volume in
# production. See README.md "Running in a container".
#
# The directory is created and chowned BEFORE the VOLUME instruction and
# BEFORE switching to the non-root user: Docker initializes a fresh named
# volume by copying the image's existing content/ownership at that path,
# so this is what makes an empty named/anonymous volume writable by the
# portunus user on first use instead of defaulting to root:root (a real
# bug this Dockerfile's own live-proof pass caught before shipping).
ENV PORTUNUS_HOME=/home/portunus/.portunus
RUN mkdir -p "$PORTUNUS_HOME" && chown -R portunus:portunus /home/portunus
VOLUME ["/home/portunus/.portunus"]

USER portunus
WORKDIR /home/portunus

ENTRYPOINT ["portunus"]
CMD ["--help"]
