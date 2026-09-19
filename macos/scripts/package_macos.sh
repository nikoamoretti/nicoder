#!/bin/bash
# Build a launchable Local Coder.app from this checkout.
# Usage: macos/scripts/package_macos.sh
set -euo pipefail

ROOT="$(cd "$(dirname "$0")/../.." && pwd)"
DIST="$ROOT/dist/macos"
APP="$DIST/Local Coder.app"
CONTENTS="$APP/Contents"
MACOS="$CONTENTS/MacOS"
RES="$CONTENTS/Resources"
SWIFT_PKG="$ROOT/macos/LocalCoder"
VERSION="0.4.0"
PYTHON="${LOCAL_CODER_PYTHON:-/usr/local/bin/python3}"
DURABLE_VENV="${LOCAL_CODER_VENV:-$HOME/.local/share/local-coder-venv}"

echo "Building Local Coder $VERSION from $ROOT"

cd "$ROOT"
if [[ ! -x "$PYTHON" ]]; then
  echo "error: $PYTHON not found" >&2
  exit 1
fi

if [[ -x "$ROOT/.venv/bin/python" ]]; then
  BUILD_PY="$ROOT/.venv/bin/python"
else
  BUILD_PY="$PYTHON"
fi
"$BUILD_PY" -m pip install --quiet build
"$BUILD_PY" -m build
if [[ ! -f "$ROOT/macos/AppIcon.icns" ]]; then
  "$BUILD_PY" "$ROOT/macos/scripts/generate_icon.py"
fi
WHEEL="$ROOT/dist/local_coder-${VERSION}-py3-none-any.whl"
if [[ ! -f "$WHEEL" ]]; then
  echo "error: expected $WHEEL" >&2
  exit 1
fi

if [[ ! -d "$DURABLE_VENV" ]]; then
  "$PYTHON" -m venv "$DURABLE_VENV"
fi
"$DURABLE_VENV/bin/pip" install --upgrade --force-reinstall "$WHEEL"
mkdir -p "$HOME/.local/bin"
ln -sf "$DURABLE_VENV/bin/local-coder" "$HOME/.local/bin/local-coder"

swift build -c release --package-path "$SWIFT_PKG"
BIN="$(swift build -c release --package-path "$SWIFT_PKG" --show-bin-path)/LocalCoder"

rm -rf "$APP"
mkdir -p "$MACOS" "$RES"
cp "$BIN" "$MACOS/Local Coder"
cp "$ROOT/macos/Info.plist" "$CONTENTS/Info.plist"
cp "$WHEEL" "$RES/"
cp -R "$ROOT/docker" "$RES/docker"

if [[ -f "$ROOT/macos/AppIcon.icns" ]]; then
  cp "$ROOT/macos/AppIcon.icns" "$RES/AppIcon.icns"
fi

chmod +x "$MACOS/Local Coder"
codesign --force --deep --sign - "$APP"
xattr -cr "$APP" || true

USER_APPS="$HOME/Applications"
mkdir -p "$USER_APPS"
rm -rf "$USER_APPS/Local Coder.app"
cp -R "$APP" "$USER_APPS/Local Coder.app"
codesign --force --deep --sign - "$USER_APPS/Local Coder.app"

echo "Built:"
echo "  $APP"
echo "  $USER_APPS/Local Coder.app"
echo "Open with Finder, or: open \"$USER_APPS/Local Coder.app\""
