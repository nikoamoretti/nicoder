# AGENTS.md — Local Coder

## Product invariants

1. **Default engine is general coding.** Ordinary app use is Local Coder apply
   plus local llama.cpp: classify talk / look / act, then a tiny Local Coder
   harness (candidate-only read / write / hash-anchored edit, no host bash)
   writes onto a disposable candidate, then preview / Keep / Discard. Do not
   call Pi, Oh My Pi, or OpenCode on the default path. Bonsai 2 27B is the
   preferred local model when it is installed; do not steer ordinary use to
   Qwen. OpenCode stays pinned but is not on the default path. The old
   eight-generation Python FIND/PATCH/CHECK/DONE loop is a legacy path only.
   General-engine budgets are request wall-clock and generation ceilings in
   `general/budgets.py`, not the old four-file span cap.
   One hosted model is also offered: NVIDIA's Nemotron 3.5 Super 120B
   (`nemotron35-super` in the catalog), chosen explicitly in Settings. When it
   is selected, talk and act calls go to NVIDIA instead of llama.cpp and no
   local server starts. Nothing ever falls back between local and hosted.
2. **Software owns truth.** Only the controller mutates `EpisodeState`. The model
   cannot write state, pick commands, or declare tests green. A model's "solved"
   is a validation request, nothing more.
3. **Fresh briefing, no transcript growth.** `briefing.py` re-renders from
   canonical state every turn. Raw logs stay in artifacts; the model sees
   compact structured observations. Hypotheses are labeled proposals, not facts.
4. **Real execution is the authority.** CHECK runs registered pytest scopes and
   parses JUnit identities. Zero tests, missing tests, new skips, corrupt
   reports, and printed "PASS" are never success.
5. **Disposable candidates.** PATCH applies atomically to a snapshot copy.
   The original tree changes only via explicit human `accept`. No partial
   applies, no fuzz, no silent rewrites; reverse data is always kept.
6. **No fake success states.** Fake runs are labeled FAKE. Unavailable
   isolation/models/hardware are BLOCKED/NOT RUN. Never silently fall back to
   unrestricted host execution. Never claim 16GB suitability without measuring
   on the target hardware.

## Architecture boundaries

- `src/local_coder/protocol.py` — strict four-action schema, unknown fields rejected.
- `src/local_coder/controller.py` — state machine, budgets, repetition guard.
- `src/local_coder/skills/` — deterministic FIND/PATCH/CHECK/DONE execution.
- `src/local_coder/retrieval/` — literal/symbol/traceback ranking, no embeddings in v1.
- `src/local_coder/execution/` — backend interface; `trusted-local` is explicitly
  non-sandboxed; `isolated` must prove real restrictions or report BLOCKED.
- `src/local_coder/inference/` — `fake.py` for offline tests, `llama_cpp.py` for
  loopback live inference. No hidden model calls behind repair/summarization.
- `src/local_coder/persistence.py` + `briefing.py` + `handoff.py` — evidence layer.
- `src/local_coder/profiles.py` — frozen named profiles (`d2` default, `d1`
  fallback). Never auto-switch a profile; resume refuses a mismatch.
- `src/local_coder/machine.py` — machine-local config (`~/.config/local-coder`),
  never committed, never contains secrets. A hosted model stores only its
  provider, endpoint, and the *name* of its API key.
- `src/local_coder/hosted.py` — the hosted-model client (NVIDIA). Reads the key
  at request time from the environment or the Keychain item `key:<NAME>` (made
  by Nico's `key` tool via /usr/bin/security, so no password prompt); the key
  never reaches config, logs, events, or errors. Streams, then folds the reply
  into llama.cpp's non-streaming shape; retries once only on a dropped/busy reply.
- `src/local_coder/server.py` — server lifecycle with explicit ownership; never
  kill a process this tool did not start, never take over an occupied port.
- `src/local_coder/cli.py` — installed `local-coder` entry point; `accept` is
  transactional, `discard` preserves evidence unless `--with-evidence`.
- `src/local_coder/accept.py` — single accept/discard implementation for CLI and app.
- `src/local_coder/app_service.py` + `app_rpc.py` — structured Mac-app API
  (JSON-lines helper, no network server).
- `src/local_coder/general/harness.py` — candidate-only read / write / hash-edit.
  No host bash. No foreign agent CLI.
- `macos/` — SwiftUI Local Coder.app. Do not add a web backend.

## Coding conventions

- Small, composable, typed functions. Pydantic 2 models at source boundaries.
- A docstring per module and public function; no comments that restate code.
- Ruff (line length 120), mypy clean on `src/local_coder/`.
- Boring architecture: stdlib + pydantic + httpx + pytest. No vector DB, no agents
  framework, no web backend. The Mac UI is a local SwiftUI app over stdin/stdout.

## Commands

```bash
python3 -m venv .venv && .venv/bin/python -m pip install -r requirements.lock
.venv/bin/python -m pytest tests/ -q
.venv/bin/python -m ruff check .
PYTHONPATH=src .venv/bin/python -m mypy src/local_coder
.venv/bin/pip install .   # local-coder CLI; entry point local_coder.cli:main
```

## Testing expectations

- No live calls in the default suite (`LOCAL_CODER_LLAMA_URL` gates live tests).
- Every rejection path (protocol, path, budget, stale, validation) has a test.
- Fixture gold diffs live outside fixture repos and are pinned byte-identical
  to the fake demo constants that mirror them.
- The fixture repos under `fixtures/` must stay byte-identical; episodes only
  ever write to disposable candidates under the state dir.
