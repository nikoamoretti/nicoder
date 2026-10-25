#!/bin/bash
# Build macos/AppIcon.icns from the app's own drawing of the meditating hominin (Nico, 2026-10-25).
set -euo pipefail
ROOT="$(cd "$(dirname "$0")/../.." && pwd)"
SRC="$ROOT/macos/LocalCoder/Sources"
TMP="$(mktemp -d)"
trap 'rm -rf "$TMP"' EXIT
swiftc -O -o "$TMP/make-icon" "$ROOT/macos/scripts/icon/main.swift" "$SRC/HomininWalker.swift" "$SRC/DockFigure.swift"
"$TMP/make-icon" "$TMP"
ICONSET="$TMP/AppIcon.iconset"
mkdir -p "$ICONSET"
for size in 16 32 64 128 256 512 1024; do
  sips -z "$size" "$size" "$TMP/icon-1024.png" --out "$ICONSET/icon_${size}x${size}.png" >/dev/null
  if [ "$size" -le 512 ]; then
    sips -z $((size * 2)) $((size * 2)) "$TMP/icon-1024.png" --out "$ICONSET/icon_${size}x${size}@2x.png" >/dev/null
  fi
done
iconutil -c icns "$ICONSET" -o "$ROOT/macos/AppIcon.icns"
mkdir -p "${PREVIEW_DIR:-$TMP}"
cp "$TMP"/icon-1024.png "$TMP"/dock-*.png "${PREVIEW_DIR:-$TMP}/" 2>/dev/null || true
echo "wrote $ROOT/macos/AppIcon.icns"
