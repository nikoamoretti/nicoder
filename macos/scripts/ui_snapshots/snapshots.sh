#!/bin/bash
# Render Local Coder's screens offscreen with sample data into PNGs (nothing appears on screen).
# Usage: macos/scripts/ui_snapshots/snapshots.sh <out-dir> [--light]
set -euo pipefail
HERE="$(cd "$(dirname "$0")" && pwd)"
ROOT="$(cd "$HERE/../../.." && pwd)"
SRC="$ROOT/macos/LocalCoder/Sources"
BUILD="$(mktemp -d)"
files=$(ls "$SRC"/*.swift | grep -v LocalCoderApp.swift)
xcrun swiftc -O -swift-version 5 -target arm64-apple-macos26.0 -module-name LocalCoder $files "$HERE/main.swift" \
  -o "$BUILD/snap"
mkdir -p "$1"
LOCAL_CODER_REPO="$ROOT" "$BUILD/snap" "$1" "${@:2}"
rm -rf "$BUILD"
