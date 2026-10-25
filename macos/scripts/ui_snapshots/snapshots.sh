#!/bin/bash
# Render Local Coder's screens offscreen with sample data into PNGs (nothing appears on screen).
# Usage: macos/scripts/ui_snapshots/snapshots.sh <out-dir> [--light]
# Lines that start with MEASURE are numbers read off the real views (column widths, row heights, sheet position).
set -euo pipefail
# A locked screen captures nothing. (grep reads all of ioreg's output: with -q and pipefail, ioreg's SIGPIPE hides a match.)
if ioreg -l -w0 | grep '"IOConsoleLocked" = Yes' > /dev/null; then
  echo "The Mac is locked, so nothing can be captured. Unlock it and run this again." >&2
  exit 2
fi
HERE="$(cd "$(dirname "$0")" && pwd)"
ROOT="$(cd "$HERE/../../.." && pwd)"
SRC="$ROOT/macos/LocalCoder/Sources"
BUILD="$(mktemp -d)"
files=$(ls "$SRC"/*.swift | grep -v LocalCoderApp.swift)
xcrun swiftc -O -swift-version 5 -target arm64-apple-macos26.0 -module-name LocalCoder $files "$HERE/main.swift" "$HERE/items.swift" \
  -o "$BUILD/snap"
mkdir -p "$1"
LOCAL_CODER_REPO="$ROOT" "$BUILD/snap" "$1" "${@:2}"
rm -rf "$BUILD"
