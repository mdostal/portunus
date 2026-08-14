#!/usr/bin/env bash
# Copies the Next.js `standalone` build (ui/.next/standalone) into
# src-tauri/resources/web/, bundled into the .app via tauri.conf.json's
# bundle.resources -- so the installed app never depends on the git
# checkout's own path still existing (design-discussion.md §2).
#
# Standalone output does NOT auto-include public/ or .next/static -- both
# must be copied alongside the standalone server manually, Next.js's own
# documented requirement (same note ui/next.config.mjs already carries).
set -euo pipefail

cd "$(dirname "${BASH_SOURCE[0]}")/.."  # ui/

if [ ! -f ".next/standalone/server.js" ]; then
  echo "error: .next/standalone/server.js not found -- run 'npm run build' first" >&2
  exit 1
fi

DEST="src-tauri/resources/web"
rm -rf "$DEST"
mkdir -p "$DEST"

cp -R .next/standalone/. "$DEST/"
mkdir -p "$DEST/.next"
cp -R .next/static "$DEST/.next/static"
if [ -d "public" ]; then
  cp -R public "$DEST/public"
fi

echo "staged Next.js standalone build -> $DEST"
