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
  project's sticky working copy (`live/candidate`) back to where the request
  started: the result it was built on while that still waits (copied, never linked),
  else its own baseline; a request with no files of its own leaves the copy as it is.
  Evidence (`meta.json`, `candidate/`) stays on disk. A cancelled run ends with
  one `stopped` event, never `done`. A request starts the sticky copy from the folder
  as it is now, unless it is built on an older request that still waits (`built_on`
  in its meta.json, found by comparing the copy with that request's result: the
  newest request that is neither discarded nor without a result of its own decides,
  and when such requests were passed over the whole copy must equal that result).
  Every engine does this, the hosted agent included: its agent starts in the copy as that
  result left it (`_run_agent_request` re-syncs the copy from that request's
  candidate, copied and never linked, else from the baseline; the re-sync stays
  because it undoes the preview's SWC rewrite of `package.json` and
  `vite.config.ts`, which the agent's review and Keep must not carry; the local
  path still does, until the `swc-rewrite` cut). Such a request takes that
  result's baseline as its own (`baseline/` and `baseline.json` copied whole; if
  either is damaged it starts from the folder instead), so its diff and its Keep
  are measured from one starting point: what Nico changed in the folder meanwhile
  is not shown as the model's work and Keep never reverts it, and a file he and
  the stack both changed makes Keep refuse (`conflict`) and write nothing. A file
  rename is checked at both ends from the snapshot indexes (never `Path.exists()`:
  the disk ignores case): the destination must be free or hold the text the request
  wrote, the source must still be there unchanged unless he already made the move
  himself, and a link at either end refuses. `apply_delta` finds a result with no
  files left while its baseline lists some, clashes, a link, a folder at a
  rename's destination and a path leaving the project before its first write,
  because `_rollback` restores bytes only; a plain file where a folder
  must be made, a folder where an added file goes, a looping link and disk errors
  are found only while writing and rolled back (a deleted file comes back 0644,
  until the `keep-late-failures` cut). A clash refusal names the
  files and what happened to each (`snapshot.conflict_sentence`: "Not kept: since
  these changes were started, src/a.ts was changed in your project. Your project
  was not changed."; at most three names, then a count); `apply_delta`'s own
  `detail` stays as it is for the MCP apply. Keep never empties the project: when
  the baseline lists files and the candidate lists none (`candidate/` gone,
  emptied or unreadable, or a request that deleted every file),
  `apply_delta` refuses before any write, for Keep and the MCP apply alike
  (`snapshot.would_empty_project`, code `empty_result`: "This result has no files
  left, so Keep would empty your project. Your project was not changed."); the
  card then has `can_keep` false and `can_discard` true, says the same sentence
  and keeps its files and diff. Known limit (the `frozen-candidate-index` cut): a
  candidate missing one file, or with one unreadable subfolder, still deletes
  those files under a normal card. Known limit: what he edited
  after the first request started is not in the copy a follow-up's agent works
  on (Keep still refuses a real clash). A continuation (`continue_from`) never
  inherits a baseline (`prepare_workspace(carry=not continue_from)`), and
  `run_general_request` decides the engine once. Keeping a request closes every
  waiting request it was built on, nearest first, whether or not the Keep wrote
  all of their files: their results are the base of the kept one's (`kept` plus
  `kept_with`). The walk stops at the first request with no files (it published
  its starting copy, so a request that continues it carries nothing below it); a
  discarded request stays discarded; a refused Keep closes nothing; a record that
  cannot be saved stays as it was. What the Keep did not write of a closed request
  the kept one took back: that is its `dropped` list, the request's files minus the
  Keep's `applied` (the snapshot gets `dropped`, a sorted list, `[]` when all went
  in). With none, the card says "These changes went in with a later request you
  kept." and Keep and Discard answer "These changes are already in your project."
  (and "..., so they cannot be discarded.", code `kept`). With some, History says
  `replaced` and the card, Keep and Discard (code `kept`) say "These changes were
  replaced by a later request you kept. Nothing from them went into src/gen.py."
  (at most three files named, then a count; `request_state.replaced_sentence`).
  All of it is per file, never by text: a file the kept request changed again
  counts as carried, so an undo plus an edit in one file, a removed function, or a
  delete then a re-create still reads "went in" (a named limit, the `closed-words`
  cut): "a kept change is in the project" means a Keep wrote its files, not that
  every line is still there. A request that is still open in its record although a
  kept request was built on it (an older build, or a record that could not be
  saved) reads as closed, from the kept request's record, in the snapshot, Keep,
  Discard, History and the note a question gets (`coordinator.records_as_read`;
  nothing is rewritten, and when either result cannot be read it stays as it was).
  While a newer request built on it is running or still has
  changes to decide, an older one can be neither kept nor discarded, and its
  snapshot says so (`can_keep` and `can_discard` false, `blocked_by` names the
  newer request, the summary says to let it finish or to Keep or Discard it first;
  its files and diff still show). A request built on an older one that ends with the
  working copy exactly as that result has no result of its own (decided from the two
  source trees in `_publish_candidate`, never from the model's words; its baseline is
  published instead): no files, no Keep or Discard, no Local Coder check or preview, it
  does not hold the older one back, its card forgets the check verdicts (the check log stays as evidence), and it keeps its
  History word (`error`, `stopped`, `review`). The next request passes over such
  requests and over discarded ones, and a question still finds the waiting result
  behind them (`waiting.LOOK_AT`, 30). Known limits: a visible junk file (such as
  `.coverage`), or a local request after a hosted one on a Babel project (the local
  path's SWC rewrite), makes it a real stack as before; a run that crashes leaves its
  stray files in the copy, so the next request starts from the folder.
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
  Settings window. A kept request, or one held back by a newer request, has no
  Keep/Discard bar: a strip with the engine's sentence and the cost takes its
  place. New Chat archives the old transcript (`chats/archive/`); closing a
  project never stops a run or clears its chat.
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
