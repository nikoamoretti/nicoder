# Mac app implementation plan (v0.3.0)

Product: a launchable **Local Coder.app** that uses the existing bounded
Python engine. Nico opens the app, chooses a folder, describes a request,
watches real progress, reviews a diff, and keeps or discards it.

## Decision

Native **SwiftUI** application (Xcode/Swift available) plus a small
**JSON-lines helper** (`python -m local_coder.app_rpc`). No web server,
no second framework, no duplicate state machine.

The helper calls one new library surface (`local_coder.app_service`) that
reuses Controller, EpisodeStore, server lifecycle, IsolatedBackend, and a
single extracted accept/discard implementation.

## Why this shape

- Finder launch needs a real `.app`, not a Terminal command.
- SwiftUI gives a normal Mac folder picker, window, and Dock icon.
- The engine stays in Python. The UI never parses pretty CLI text.
- A loopback HTTP API would expand the attack surface; stdin/stdout is
  enough and stays inside the app process tree.

## Vertical slice (build first)

1. Open app → recent projects / Open project.
2. Inspect the folder; say plainly if Python/pytest work is supported.
3. Natural-language request box.
4. Start the owned local model if needed.
5. Run one episode; show progress from engine events.
6. Show controller-owned summary, files, diff, and checks.
7. Keep (existing accept gate) or Discard (original tree untouched).

Then: Stop, history, quit cleanup, prerequisite actions, packaging.

## Integration rules

- Default profile `d2`. Profile choice lives in Advanced settings.
- Do not write `local-coder.toml` unless the user confirms. Standard
  Python/pytest trees can use engine defaults (isolated execution).
- Never commit, stash, reset, or overwrite the selected repo.
- Dirty git / source drift: explain and refuse accept.
- One active editing episode per project.
- Cancel uses the adapter's existing stream cancel plus a controller
  check between steps.
- On quit: cancel the active episode, stop only a server this app
  started, preserve evidence.
- Finder PATH is incomplete; the helper adds Homebrew, Docker Desktop,
  and `/usr/local/bin` itself.

## Packaging

- Version **0.3.0** (v0.2.0 CLI/wheel identities stay in the release
  record).
- `macos/scripts/package_macos.sh` builds `Local Coder.app`.
- The app installs/upgrades the bundled wheel into
  `~/.local/share/local-coder-venv` using `/usr/local/bin/python3`.
- Ad-hoc codesign only. No notarization claim.

## Verification

Offline tests for inspect/classify/accept/progress/cancel/RPC.
Live demos through the app on disposable copies: one bug fix, one
small feature. Keep unsuccessful runs. Measure responsiveness and
memory on this Mac without claiming a general 16GB guarantee.
