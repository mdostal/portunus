#!/usr/bin/env bash
# Portunus one-command installer.
#
#   curl -fsSL https://mdostal.github.io/portunus/install.sh | bash
#
# This is the CANONICAL copy -- it lives in the repo at scripts/install.sh and
# gets published as-is to the gh-pages site root. If you're editing the
# published copy directly, edit here instead and re-publish.
#
# Installs the CLI + MCP server, then wires it into whatever AI coding agent
# CLIs are already on this machine (Claude Code, Codex CLI today) --
# `portunus agent init` owns that part and is idempotent, safe to re-run.
#
# Not yet on PyPI under this name -- "portunus" there is an unrelated,
# unmaintained package (github.com/IQTLabs/portunus). This installs straight
# from GitHub until a real PyPI release ships under `pantheon-portunus`
# (pyproject.toml's actual distribution name; the installed command is still
# just `portunus`).
set -euo pipefail

REPO="git+https://github.com/mdostal/portunus.git"

if ! command -v python3 >/dev/null 2>&1; then
  echo "error: python3 (>=3.9) is required and wasn't found on PATH." >&2
  exit 1
fi

if ! command -v pipx >/dev/null 2>&1; then
  echo "-- pipx not found, installing it via pip --user"
  python3 -m pip install --user --quiet pipx
  python3 -m pipx ensurepath >/dev/null 2>&1 || true
  # pipx's shims may not be on PATH yet in this shell -- fall back to the
  # standard location rather than requiring the user to restart their shell
  # mid-install.
  export PATH="$HOME/.local/bin:$PATH"
fi

echo "-- installing portunus from GitHub"
pipx install --force "$REPO"

if ! command -v portunus >/dev/null 2>&1; then
  export PATH="$HOME/.local/bin:$PATH"
fi

if ! command -v portunus >/dev/null 2>&1; then
  echo "error: portunus installed but isn't on PATH. Add \$HOME/.local/bin to your PATH and re-run." >&2
  exit 1
fi

echo "-- wiring up any agent CLIs already on this machine"
portunus agent init

echo
echo "Done. 'portunus' is installed and wired into every agent CLI detected above."
echo "Next: 'portunus agent status' any time to see what's registered, or"
echo "      https://github.com/mdostal/portunus#readme for the full picture."
