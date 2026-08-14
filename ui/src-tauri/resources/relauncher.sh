#!/usr/bin/env bash
# Swaps the installed Portunus.app for a freshly-downloaded one. Spawned as
# a DETACHED process by the running app right before it quits -- a process
# cannot safely overwrite its own running binary, so this is the standard
# self-update pattern (spawn-a-helper-then-exit).
#
# Safety, not an afterthought (design-discussion.md §4/§8, story 03 AC 4-5):
# the new bundle is copied and sanity-checked (Info.plist + codesign)
# BEFORE the old one is touched at all. The actual swap renames the old
# app aside as a backup rather than deleting it outright -- the backup is
# only removed after the new app is confirmed in place, and is restored on
# any failure. A corrupted/incomplete download must never destroy a
# working install.
#
# Args: $1 = path to the downloaded new-app .zip
#       $2 = path to the currently-installed .app to replace
#       $3 = PID of the Portunus process to wait for exit (so we don't
#            race a swap while the old binary is still running/holding
#            file locks)
set -uo pipefail

NEW_ZIP="$1"
INSTALLED_APP="$2"
PARENT_PID="$3"
LOG="/tmp/portunus-relauncher.log"

log() { echo "[$(date '+%Y-%m-%d %H:%M:%S')] $*" >> "$LOG"; }

log "starting: new_zip=$NEW_ZIP installed_app=$INSTALLED_APP parent_pid=$PARENT_PID"

# Wait (bounded) for the parent process to actually exit before touching
# anything -- avoids racing a swap against a still-running old binary.
for _ in $(seq 1 100); do
  if ! kill -0 "$PARENT_PID" 2>/dev/null; then
    break
  fi
  sleep 0.1
done

WORKDIR=$(mktemp -d)
trap 'rm -rf "$WORKDIR"' EXIT

if ! unzip -q "$NEW_ZIP" -d "$WORKDIR"; then
  log "FAILED: unzip of $NEW_ZIP failed -- aborting, original app untouched"
  exit 1
fi

NEW_APP=$(find "$WORKDIR" -maxdepth 1 -name "*.app" -type d | head -1)
if [ -z "$NEW_APP" ]; then
  log "FAILED: no .app bundle found inside $NEW_ZIP -- aborting, original app untouched"
  exit 1
fi

if [ ! -f "$NEW_APP/Contents/Info.plist" ]; then
  log "FAILED: $NEW_APP has no Info.plist -- not a valid app bundle, aborting, original app untouched"
  exit 1
fi

if ! codesign -dv "$NEW_APP" >/dev/null 2>&1; then
  log "FAILED: codesign check failed on $NEW_APP -- aborting, original app untouched"
  exit 1
fi

log "verified new bundle at $NEW_APP -- proceeding with swap"

BACKUP="${INSTALLED_APP}.bak"
rm -rf "$BACKUP"

if [ -d "$INSTALLED_APP" ]; then
  if ! mv "$INSTALLED_APP" "$BACKUP"; then
    log "FAILED: could not move $INSTALLED_APP aside -- aborting, original app untouched"
    exit 1
  fi
fi

# PORTUNUS_RELAUNCHER_TEST_FAIL_SECOND_MV is a test-only hook (never set in
# real use) letting the test suite deterministically exercise the restore-
# from-backup path below -- this is the single highest-risk step in the
# whole epic (story 03's own risk rating), so it gets a real failure-path
# test, not just a happy-path one.
if [ "${PORTUNUS_RELAUNCHER_TEST_FAIL_SECOND_MV:-}" = "1" ]; then
  log "TEST HOOK: simulating second-mv failure"
  SECOND_MV_OK=1
else
  mv "$NEW_APP" "$INSTALLED_APP"
  SECOND_MV_OK=$?
fi

if [ "$SECOND_MV_OK" != "0" ]; then
  log "FAILED: could not move new app into place -- restoring original from backup"
  if [ -d "$BACKUP" ]; then
    rm -rf "$INSTALLED_APP"
    mv "$BACKUP" "$INSTALLED_APP"
    log "restored original app from backup"
  fi
  exit 1
fi

# Swap succeeded -- safe to drop the backup now.
rm -rf "$BACKUP"
log "swap complete -- relaunching $INSTALLED_APP"

open "$INSTALLED_APP"
log "relaunch requested, exiting cleanly"
