#!/bin/bash
# Build a launchable Nicoder.app from this checkout (the app was Local Coder until 2026-10-25; the engine,
# CLI and data folders keep the local-coder name, and the bundle id stays app.localcoder.desktop so macOS keeps
# its folder answers and settings).
# Usage: macos/scripts/package_macos.sh
set -euo pipefail

ROOT="$(cd "$(dirname "$0")/../.." && pwd)"
DIST="$ROOT/dist/macos"
APP="$DIST/Nicoder.app"
CONTENTS="$APP/Contents"
MACOS="$CONTENTS/MacOS"
RES="$CONTENTS/Resources"
SWIFT_PKG="$ROOT/macos/LocalCoder"
VERSION="0.4.0"
PYTHON="${LOCAL_CODER_PYTHON:-/usr/local/bin/python3}"
DURABLE_VENV="${LOCAL_CODER_VENV:-$HOME/.local/share/local-coder-venv}"

# Sign with a stable identity so macOS remembers the folder access granted to Local Coder across updates. An ad-hoc
# signature ("-") changes with every build, so macOS asked again for Desktop, Documents and Downloads after each
# install. LOCAL_CODER_SIGN_IDENTITY overrides; without an Apple Development identity the build stays ad-hoc.
SIGN_ID="${LOCAL_CODER_SIGN_IDENTITY:-$(security find-identity -v -p codesigning 2>/dev/null \
  | sed -n 's/.*"\(Apple Development: [^"]*\)".*/\1/p' | head -1)}"
SIGN_ID="${SIGN_ID:--}"

echo "Building Nicoder $VERSION from $ROOT (signing: $SIGN_ID)"

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
cp "$BIN" "$MACOS/Nicoder"
cp "$ROOT/macos/Info.plist" "$CONTENTS/Info.plist"
cp "$WHEEL" "$RES/"
cp -R "$ROOT/docker" "$RES/docker"

if [[ -f "$ROOT/macos/AppIcon.icns" ]]; then
  cp "$ROOT/macos/AppIcon.icns" "$RES/AppIcon.icns"
fi

chmod +x "$MACOS/Nicoder"
codesign --force --deep --sign "$SIGN_ID" "$APP"  # fails here, before the installed app is touched
xattr -cr "$APP" || true

USER_APPS="$HOME/Applications"
mkdir -p "$USER_APPS"
rm -rf "$USER_APPS/Nicoder.app"
cp -R "$APP" "$USER_APPS/Nicoder.app"
codesign --force --deep --sign "$SIGN_ID" "$USER_APPS/Nicoder.app"
rm -rf "$USER_APPS/Local Coder.app"  # the same app under its old name

echo "Built:"
echo "  $APP"
echo "  $USER_APPS/Nicoder.app"
echo "Open with Finder, or: open \"$USER_APPS/Nicoder.app\""
