# Local Coder Mac application

This directory is the native SwiftUI wrapper around the local engine.
Ordinary use is: open **Local Coder.app**, start or open a project, describe
a request, watch a preview, then keep or discard the change. The default
engine is Local Coder apply plus local llama.cpp (Bonsai when installed).
OpenCode is pinned but not used on that path. The older Python-only repair
loop is an advanced setting.

## Build

From the repository root:

```bash
macos/scripts/package_macos.sh
```

That command:

1. Builds the `0.4.0` Python wheel.
2. Installs it into `~/.local/share/local-coder-venv` (the durable engine).
3. Compiles the SwiftUI app.
4. Writes an ad-hoc-signed `Local Coder.app` to `dist/macos/` and copies it
   to `~/Applications/Local Coder.app`.

Requirements: Xcode command-line tools, `/usr/local/bin/python3` (3.11+),
and the existing local model + llama.cpp install. Docker Desktop is required
for isolated tests.

The app is **not notarized**. First launch from Finder may require
right-click → Open if Gatekeeper asks. Do not turn macOS security off.

## Layout

- `LocalCoder/` — Swift package
- `Info.plist` — bundle metadata
- `scripts/package_macos.sh` — reproducible package step

The UI talks to `python -m local_coder.app_rpc` over stdin/stdout. There is
no network server.
