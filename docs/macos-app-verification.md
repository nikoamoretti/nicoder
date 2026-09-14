# Local Coder.app v0.3.0 — verification

Date: 2026-09-14 (Pacific). Checkout after this work. Hardware: Apple M2 Pro,
16GB unified memory. This is evidence from this machine, not a general 16GB
guarantee.

## What was built

A launchable Mac application that uses the existing bounded engine.

| Item | Location |
|---|---|
| Everyday app | `/Users/nico-yardlogix/Applications/Local Coder.app` |
| Repo copy | `/Users/nico-yardlogix/projects/local-coder-16gb/dist/macos/Local Coder.app` |
| Rebuild | `macos/scripts/package_macos.sh` |
| Engine API | `src/local_coder/app_service.py`, `app_rpc.py` |
| Version | 0.3.0 (v0.2.0 wheel identities are unchanged in `pilot/reports/v0.2.0/`) |

Signing: ad-hoc only. Not notarized. First Finder launch may need
right-click → Open. macOS security settings were not turned off.

The UI talks to `python -m local_coder.app_rpc` over stdin/stdout. There is
no network server.

## Offline software

```text
.venv/bin/python -m ruff check .          # clean
PYTHONPATH=src .venv/bin/python -m mypy src/local_coder
.venv/bin/python -m pytest tests/         # 275 collected tests; offline run passed
```

New coverage: project inspect/classify/explain, progress phrases, app-service
run/keep/discard with `fake:success`, cancel, JSON-lines RPC. Existing CLI
accept/discard tests still pass; accept now lives in `local_coder.accept`.

GitHub CI remains blocked by the same account billing issue as v0.2.0. That
was not treated as a local-app blocker.

## Finder launch

`open "/Users/nico-yardlogix/Applications/Local Coder.app"` started the
packaged binary and the helper (`python -m local_coder.app_rpc`) without
`PYTHONPATH`, without an activated developer venv, and without a Terminal
session. Screenshots: `docs/screenshots/welcome.png`,
`docs/screenshots/folder-picker.png`.

The first packaged SwiftUI `WindowGroup` produced an empty window. The
shipping app hosts SwiftUI in an AppKit `NSHostingController`. Quit now
times out instead of hanging if the helper does not answer.

## Live coding through the app API

Same durable interpreter the app uses
(`~/.local/share/local-coder-venv`, package 0.3.0). Profile `d2`. Isolated
Docker worker `local-coder-worker:1`. Disposable copies under
`/tmp/local-coder-app-demos/`. The local model chose the edits. No gold
patch was injected.

Acceptance defined before the runs:

- Bug fix: `add(2, 3) == 5`, `add(-1, -1) == -2`, `mul` unchanged.
- Feature: `greet("") == "Hello!"`, `greet("Ada") == "Hello, Ada!"`.

| Demo | Episode | Outcome | Gens | Time | Afterward |
|---|---|---|---|---|---|
| Bug fix (`bugfix-calc`) | `app-live-bugfix` | solved, final pass | 4 / 8 | 37.5 s | Keep applied `src/calc.py` (`return a + b`) |
| Feature (`feature-greet`) | `app-live-feature` | solved, final pass | 4 / 8 | 28.7 s | Discard left original `greet("") == "Hello, !"` |

Candidate diffs (controller-owned):

```diff
--- a/src/calc.py
+++ b/src/calc.py
@@ -2,7 +2,7 @@
 def add(a, b):
     """Return the sum of a and b."""
-    return a - b  # BUG: subtracts instead of adding
+    return a + b
```

```diff
--- a/src/greet.py
+++ b/src/greet.py
@@ -3,4 +3,6 @@
 def greet(name: str) -> str:
     """Return a greeting for a person."""
+    if not name:
+        return "Hello!"
     return f"Hello, {name}!"
```

The model’s DONE text on the bug-fix run claimed add was “already correctly
implemented.” The recorded diff and tests are the authority. The app keeps
that commentary labeled separately.

These are two small disposable Python projects. They are not a general
success rate.

## Other product checks

| Check | Result |
|---|---|
| JavaScript-only folder | Unsupported: “currently edits small, testable Python projects.” |
| “add a search box” on a Python calc project | Unsupported: not a front-end engine |
| Dirty git after Keep | Inspect verdict `dirty`; edits refused |
| Stop | Fake episode cancelled; original `calc.py` unchanged |
| Explain | Read-only retrieval; `calc.py` bytes unchanged |

## Memory and responsiveness on this Mac

During the live episodes the owned llama-server RSS was about 3.1–3.3 GB.
Controller RSS about 52–54 MB. Host available memory reported around
1.8–2.3 GB of 16 GB while the model was resident. The machine was also
running Docker Desktop and other apps. Compressor/swap activity was already
high on the host; this is not a quiet-machine qualification.

The two live requests completed in 38 s and 29 s after the server was up.
The home window stayed open and clickable while those runs used the helper
API. No universal 16GB claim is made.

## Still unsupported / unfinished

- Not a general coding assistant. Python + pytest only.
- Hardware qualification procedure: still NOT RUN.
- GitHub CI: still not executed (billing).
- Accessibility names on recent-project buttons are weak, so scripted
  clicks on those buttons were unreliable. Manual Open Project works.
- The app does not commit, stash, or reset the selected repo.

Raw live JSON (local, not committed): `/tmp/local-coder-app-demos/results/`.
