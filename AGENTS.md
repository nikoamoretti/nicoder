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
   Hosted models are also offered, always chosen explicitly: NVIDIA's
   Nemotron 3.5 Super 120B (`nemotron35-super`, free) and any OrcaRouter
   model (`orca:<model id>`, Nico's choice on 2026-10-22: "Im going to use
   orcarouter"). When one is selected, talk and act calls go to that provider
   instead of llama.cpp and no local server starts. Nothing ever falls back
   between local and hosted, or between hosted models.
   With a hosted model, talk and act run **Local Coder's own agent**
   (`src/local_coder/agent/`, Nico's call on 2026-10-22: OpenCode/Conductor-level
   capability, but never OpenCode or Conductor as the engine). The model calls
   tools that Local Coder executes: find_project / list / glob / grep / read
   anywhere in the home folder except secrets and ~/Library; write / edit only
   in the request's working copy (Keep/Discard still decides what lands); bash
   only inside the macOS sandbox (`agent/sandbox.py`: writes limited to the
   working copy and temp folders, secrets unreadable, Keychain and the `key`
   tool blocked, scrubbed environment). Questions are read-only and may
   `open_project` the repo they found. Do not loosen these rules, add an
   unsandboxed shell, or route the agent through a foreign agent CLI.
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
- `src/local_coder/hosted.py` — the hosted-model client (NVIDIA, OrcaRouter).
  Reads the key at request time from the environment or the Keychain item
  `key:<NAME>` (made via /usr/bin/security, so no password prompt); the key
  never reaches config, logs, events, or errors. Streams, then folds the reply
  into llama.cpp's non-streaming shape. 5xx/dropped streams get three attempts,
  429 four; refusals (bad key, empty balance, unknown model) are not retried
  and are explained in plain words. OrcaRouter requests carry
  `X-OrcaRouter-Include-Cost` (billed `usage.cost_usd`, summed per run and shown
  in the app) and one `X-OrcaRouter-Session-Id` per agent run. Reasoning off
  (the composer's switch, Qwen and Nemotron only): `chat_template_kwargs.
  enable_thinking=false` plus `enable_thinking=false` for Qwen on OrcaRouter
  (checked live 2026-10-22: OrcaRouter ignores `reasoning` for Qwen3.8 27B).
  OrcaRouter models keep their provider's sampling defaults (no temperature).
- `src/local_coder/orcarouter.py` — OrcaRouter account and models. The key
  lives in the Keychain item `key:ORCAROUTER_API_KEY`, written by
  `/usr/bin/security -i` fed on stdin (never in a process list), registered for
  Nico's `key` tool. "Sign in with OrcaRouter" is OAuth 2.0 + PKCE with a
  127.0.0.1 callback (the helper's only listener, open while the browser
  waits); a pasted key is checked against `/v1/dashboard/billing/subscription`
  first. The model list is the public `/v1/models` joined with the public
  benchmark board (intelligence, speed), filtered to chat models that list
  `tools`, cached 12 h in `~/.config/local-coder/orcarouter-models.json`.
  The menu shows the selected and recent models (else a data-picked few); any
  other is picked in the app's model browser.
- `src/local_coder/server.py` — server lifecycle with explicit ownership; never
  kill a process this tool did not start, never take over an occupied port.
- `src/local_coder/cli.py` — installed `local-coder` entry point; `accept` is
  transactional, `discard` preserves evidence unless `--with-evidence`.
- `src/local_coder/accept.py` — single accept/discard implementation for CLI and app.
- `src/local_coder/app_service.py` + `app_rpc.py` — structured Mac-app API
  (JSON-lines helper, no network server). A request never creates its project
  folder: `general/inspect.folder_problem` refuses a path that is gone, a file or
  unreadable, in plain words, before any worker or model call (only `new_project`
  and `start_chat` make folders). Errors reach the app as plain sentences:
  `AppError` and `GeneralError` keep their own code and detail, anything
  unexpected becomes one generic sentence, and the raw text and traceback go to
  stderr and `<data dir>/logs/helper-errors.log` (`errorlog.py`).
- `src/local_coder/general/request_state.py` — the one definition of where a
  request stands: waiting (neither kept nor discarded), kept, or discarded; kept
  wins. The review snapshot, History, Keep, Discard and `general/waiting.py` all
  read it. Keep and Discard are final: a closed request offers neither, `keep`
  refuses a discarded request, `discard` refuses a kept one, and Discard puts the
  project's sticky working copy (`live/candidate`) back to the request's baseline.
  Evidence (`meta.json`, `candidate/`) stays on disk. A cancelled run ends with
  one `stopped` event, never `done`. A request can be built on an older request that
  still waits (`built_on` in its meta.json, found by comparing the sticky working
  copy with that request's result; a hosted-agent run starts from the folder and has
  none). Keeping a request closes the older ones it was built on as kept
  (`kept_with`: "These changes went in with a later request you kept."); while a
  newer request built on it still has changes to decide, an older one can be neither
  kept nor discarded.
- `src/local_coder/general/waiting.py` — a question asked while a result waits
  for Keep or Discard gets a note built from the request's own record (title,
  age, files, "not in the folder yet", and for the agent where to read the
  waiting copy, read-only). It is not saved in the chat. A waiting copy is never
  opened as a project: `open_project` refuses the data root.
- `src/local_coder/general/harness.py` — candidate-only read / write / hash-edit
  for the local llama.cpp models. No host bash. No foreign agent CLI.
- `src/local_coder/agent/` — the tool-calling agent for models with native tool
  calls (today the hosted Nemotron): `access.py` (path rules), `sandbox.py`
  (sandbox-exec profile and runner), `tools.py`, `loop.py`, `prompts.py`,
  `runner.py` (app entry points). Change requests run it in
  `coordinator._run_agent_request`; Local Coder's own checks run only on Vite
  web apps, failures go back to the agent (two rounds), other projects record
  `local_checks: not_run`. Transcripts: `agent_transcript.json` per request,
  `~/.local/share/local-coder/agent-runs/` for questions.
- `src/local_coder/mcp_server.py` + `agent/delegate.py` — delegation for other
  agents (Nico, 2026-10-22: "claude is very expensive"). A stdio MCP server,
  registered at user scope in Claude Code as `local-coder` (timeout 20 min,
  alwaysLoad), exposes `nemotron_explore` (read-only, limited to the given
  project) and `nemotron_draft_change` / `nemotron_apply_draft` /
  `nemotron_discard_draft` (a private copy under `drafts/`; apply refuses on
  conflicts). Always uses the hosted Nemotron entry, whatever the app selected.
  Standard library JSON-RPC only; keep it that way.
- `macos/` — SwiftUI Local Coder.app (macOS 26, SwiftUI app lifecycle). Do not
  add a web backend. Layout: sidebar (projects, requests), the conversation as
  a log (prompt headline, live step timeline with times and Stop, a one-line
  summary with cost, the answer as prose), a Changes inspector (files, diff,
  checks, preview, Keep/Discard), a glass composer with the model menu and the
  reasoning switch, the OrcaRouter model browser and connect sheet, and a
  Settings window. New Chat archives the old transcript
  (`chats/archive/`); closing a project never stops a run or clears its chat.
  `LOCAL_CODER_UI_PROBE=1` at launch lets a script open views (never change
  state); `macos/scripts/ui_snapshots/snapshots.sh <dir>` renders every screen
  offscreen with sample data.

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
