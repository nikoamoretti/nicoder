# Local Coder general coding — verification

Date: 2026-09-17 (Pacific). Hardware: Apple M2 Pro, 16GB. Branch
`feature/general-coding-opencode`. This is evidence from this machine, not a
finished product claim.

## Product path

Default engine is pinned **OpenCode 1.18.22** (MIT, Homebrew binary SHA-256
`33684a2aa6aad5386069fe44031f928aaa36cf860bb6e430ed7241ff92f6c8dd`) talking to
an owned **llama-server** on loopback. The old FIND/PATCH/CHECK/DONE Python
repair path remains as `engine=legacy`.

OpenCode runs with a fake `HOME` at `~/.local/share/local-coder/engine-home`.
It does not read Nico’s `~/.config/opencode` or `~/.local/share/opencode`.

## Compatibility probes (4B)

| Probe | Result |
|---|---|
| Isolated `opencode serve --pure` health | Pass. Version 1.18.22. `homes_differ=true`. |
| Effective config | `model=llama.cpp/Qwen3.5-4B-Q4_K_M`, `small_model` same, `enabled_providers=["llama.cpp"]`, `share=disabled`, `autoupdate=false`. |
| Native tool list | `bash`, `read`, `write`, `edit`, `glob`, `grep`, `todowrite`, plus denied `webfetch`/`task`. |
| llama.cpp `--jinja` tool_calls | Pass. Model emitted a structured `write_file` tool call. |
| OpenCode tool loop | Pass in 48.7s. Created `src/math_util.ts` with `return a + b` and a node assert file. Tools observed: `write`, `edit`. |

First application attempt failed for an **integration** reason: OpenCode’s
prompt was 20517 tokens against `n_ctx=16384` (`ContextOverflowError`).
Preserved as `create-overflow-v1`. Context was raised to **32768** and one
versioned create retest was run.

## Request 1 — 4B create (versioned retest)

- Model: `Qwen_Qwen3.5-4B-Q4_K_M.gguf` (SHA-256
  `13c16f426047e2de38cd075bdade4a7bcbc8c774384876f677740cda65f8a983`).
- Server: llama.cpp 10809, `--jinja --parallel 1 --ctx-size 32768`, RSS ~3.98 GB.
- Wall time: 1170.9 s (~19.5 min).
- Files the model changed: `src/App.tsx`, `src/App.css`, `package.json`,
  `package-lock.json`.
- Preview: `http://127.0.0.1:4306/` on the candidate tree.

Independent browser check (Cursor browser, 2026-09-17):

- Columns **To Do / In Progress / Done** are present in the accessibility tree.
- Add-task modal opens; title and description can be entered; Create closes it.
- **Created tasks do not appear on the board.** The model’s `COLUMNS` have no
  `status` field, so `column.status` is `undefined` and cards never match.
- Request 2 (due dates) is **blocked** because request 1 did not produce a
  working application.

The builder did not patch the generated app. 4B weights and that failed-run
evidence remain under `~/.cache/local-coder/models/` and
`~/.local/share/local-coder/evidence/4b-taskboard-2026-09-17/`.

## 9B model (approved download)

Approved file only: `bartowski/Qwen_Qwen3.5-9B-GGUF` /
`Qwen_Qwen3.5-9B-Q4_K_M.gguf` → `~/.cache/local-coder/models/`.

- On-disk size: 5.7 GB.
- SHA-256: `d784ce9eda1a5a7b51e8f705a9e6310844bf4f173654d115823c775fdea56d43`
  (checksum verified after download).
- The 4B GGUF was not deleted.
- Owned 4B llama-server was stopped before 9B was loaded.
- Machine config now aliases `Qwen3.5-9B-Q4_K_M` at `n_ctx=32768`.

## Compatibility probes (9B)

| Probe | Result |
|---|---|
| Native llama.cpp `--jinja` tool_calls | Pass. `finish_reason=tool_calls`, `write_file` with `path=/tmp/x.txt`, `content=hello`. Prompt 292 tokens. Decode ~14.6 tok/s. |
| First OpenCode request size | Pass. First create-shaped prompt was **20526 tokens** in a **32768** context (`truncated=0`). Tool-loop first prompt was **20379**. |
| Isolated OpenCode config | After pointing the probe at the machine alias: `model=llama.cpp/Qwen3.5-9B-Q4_K_M`. |
| OpenCode tool loop | Partial. In 240.8 s the model created `src/math_util.ts` via tools but left `return a - b` when the probe cap was hit. 9B is much slower than 4B. |
| Mid-session context | Fail. After bash/read tools the create session asked for **104101 tokens** (`ContextOverflowError`). One assistant message in that session is ~379 KB. Compaction did not keep the transcript under 32k. |

The first probe that still used a leftover 4B OpenCode config failed with
`ProviderModelNotFoundError` for `Qwen3.5-9B-Q4_K_M`. That is a Local Coder
config-reuse bug, not a model-quality result. The probe now passes the
machine alias into `OpenCodeServer`.

## Request 1 — 9B create

Fresh blank Vite+React+TS starter. Local model only. Builder did not edit the
generated app.

| Step | Wall time | What happened |
|---|---|---|
| Create session | 152.9 s | First prompt fit (20526 tokens). Model used bash/read. Then overflow at 104101 tokens. **Zero file changes** vs the starter. |
| Playwright request 1 | 140.0 s | **6 failed.** Page is still the empty starter. |
| Same-session feedback | 0.6 s | Immediate `session.error` (context already dead). Playwright still 6 failed (136.6 s). |
| New-session repair (same candidate, remaining create budget) | 721.3 s (12 min cap) | Model wrote `TaskBoard.tsx`, `TaskCard.tsx`, `tasks.ts`, CSS. Preview title became “Task Board”. |
| Playwright after repair | 139.2 s | **6 failed.** |
| Independent browser | — | `#root` empty. Console: `The requested module '/src/utils/tasks.ts' does not provide an export named 'Task'` (`export interface` / `export type` is erased at runtime; the model imported it as a value). |
| Last feedback (5 min remaining) | 300 s cap | Model changed `interface Task` to `type Task` (still not a runtime export). Preview later died (`ERR_CONNECTION_REFUSED`). |

Final independent reload of the candidate preview (after restarting only the
owned Vite process): **blank white page**, same `Task` export `SyntaxError`.
No To do column. Creating a task is impossible because the app does not mount.

**Creating a task does not work. Editing, moving, searching, and refresh
persistence were not reached. Request 2 (due dates / Overdue) was not run.**

Observed defects in the files the model wrote (not patched by the builder):

- Value-import of a TypeScript-only `Task` type; the module never boots.
- `addTask()` writes `localStorage` but does not update React state.
- A mount effect writes `tasks=[]` back to `localStorage`, which would wipe
  storage even if boot succeeded.
- `deleteTask` / `updateTask` in `TaskBoard` shadow the imports and recurse.

## Request 2

Not run. 9B did not produce a working request-1 application.

## Mac usability during 9B

- llama-server: `--jinja --parallel 1 -c 32768 --n-gpu-layers 99`.
- Process RSS after load ~1.1 GB; unified/wired memory was much higher
  (system-wide free often **15–17%** while 9B + OpenCode + preview ran).
- After some sessions ended, free memory briefly rose to ~61%, then returned
  to ~15% with llama still resident.
- UI remained usable enough to run Cursor and a browser, but memory was tight.
  No other models were downloaded.

## GUI package

Everyday Dock / `~/Applications/Local Coder.app` is still **0.3.0**. The 0.4.0
SwiftUI sources (New Project, preview, Keep, follow-up) are on this branch but
were **not packaged**. `xcodebuild` still requires a signed Xcode license.
That license was not accepted on Nico’s behalf, and no administrator password
was requested.

The New Project → request → preview → Keep → follow-up window chain was
therefore **not** demonstrated. An internal API/preview run is not a substitute.

## Privacy / isolation notes

- OpenCode `HOME` is app-owned. User OpenCode config still exists and was not
  overwritten.
- Cloud provider env keys are stripped from the OpenCode process.
- `webfetch` / `websearch` / `external_directory` / `task` are denied.
- OpenCode still emits many builtin `plugin.added` events and a
  `catalog.updated` event. Those are recorded; they are not treated as proof of
  cloud inference, and they are also not treated as fully offline.
- OpenCode logs still show it discovering skills under the real user
  `~/.claude/skills` and `~/.agents/skills` even with a fake `HOME`.
- llama-server is bound to `127.0.0.1`. Its log still warns that CORS defaults
  are permissive when no API key is set.

## Context bounding and isolation fix (no new live generation)

The 9B overflow happened on the OpenCode → llama.cpp `/v1/chat/completions`
hop: OpenCode assembled full tool output (one ~379 KB message; next request
104101 tokens) and llama.cpp refused it. Local Coder now owns that hop with a
loopback proxy. Full tool payloads are stored under the app data directory
(`tool-observations/`, not Git). The model sees a bounded observation with an
`OBSERVE` handle. A complete-request token check runs before every dispatch
(`input + output reserve + safety <= n_ctx`). Irreducible requests return
`context_budget_error` and do not generate. Compaction is deterministic and
local; `small_model` is the same llama.cpp alias.

Host-instruction isolation is no longer “change HOME and hope.” The owned
OpenCode 1.18.22 process sets `OPENCODE_TEST_HOME`, `OPENCODE_CONFIG_DIR`,
`OPENCODE_PURE`, and the supported disable flags for default plugins, external
skills, Claude-compatible instructions/skills/prompt, project/parent
`AGENTS.md`/`CLAUDE.md`/`CONTEXT.md` discovery, model catalog fetch, share,
and autoupdate. Nico’s real `~/.claude` and `~/.agents` are not edited.

The leftover 4B OpenCode provider mapping is refused: one resolved alias is
written into the isolated config, reuse requires the same project and model,
and a stale registered model aborts before a coding task.

This does **not** install Dock app 0.4.0. The original 9B overflowed
candidate under `trial-create-9b-1789670226` is unchanged.

## Stream contract, accounting, and recorded 9B recovery (2026-09-17 evening)

This is a **recorded recovery/continuation**, not evidence that the model
built the application from scratch. Historical candidate bytes were copied
into `trial-recover-9b-1789697755` and then left untouched.

### Compatibility (mocked OpenCode parser + live 9B)

- Proxy now streams `chat.completion.chunk` objects (`choices[].delta`).
  A buffered full completion is translated into those chunks; wrapping the
  original JSON in `data:` is rejected.
- Upstream HTTP errors stay errors. Oversized writes return HTTP 400 /
  an SSE error object, not a fake assistant completion.
- After `finish_reason`, the proxy emits `data: [DONE]` and closes the
  stream (`Connection: close`). Without that, OpenCode executed tools and
  then waited indefinitely.
- Output reserve follows the request’s effective `max_tokens` /
  `max_completion_tokens` (missing → configured default; conflict → min).
  Flattened tokenize + template slack is labeled `estimated` when
  `/tokenize` is unavailable. `n_ctx` stayed **32768**.
- Observations are clipped tool results. `local-coder-observe` is an
  explicit bash CLI; transcript text is not auto-scanned for OBSERVE.
- Tests: `tests/test_stream_contract.py` (including a real OpenCode 1.18.22
  session against a scripted mock), `tests/test_context_budget.py`,
  `tests/test_opencode_isolation.py`, recovery-copy immutability.

Live probes on the production proxy + 9B (same attempt, 2 tries):

| Probe | Tokens in | Result |
|---|---|---|
| 1 | 6686 (`llama.cpp /tokenize`, not estimated) | Native `read` of `src/App.tsx` + READY. Aborted at 180s before idle. |
| 2 | 6672 | Assistant text `READY`. Same abort-before-idle. |

Those were readable provider responses, not transport failures. Headroom
was ~23500 of 32768 after a 2048 output reserve and 512 margin.

### Recovery attempt

- Historical: `~/.local/share/local-coder/projects/app-user-cabb260102a3/requests/trial-create-9b-1789670226/` (bytes unchanged; `App.tsx` SHA-1 `184eda4e…`).
- Continuation: `.../requests/trial-recover-9b-1789697755/candidate/`
- Evidence: `~/.local/share/local-coder/evidence/9b-taskboard-recovery-2026-09-17/`
- Model: owned `Qwen3.5-9B-Q4_K_M` at `127.0.0.1:8080`, `n_ctx=32768`.
- Engine: OpenCode 1.18.22 through the corrected proxy. Local inference only.
- Generations: **25 / 30**. Elapsed: **~41 min / 45**. CREATE watch 20 min
  exhausted during the last turn (no extra trial campaign).
- Live request sizes: 6672–16102 prompt tokens vs 32768. Last turn
  `remaining_headroom=14106`, `output_source=max_tokens`, `estimated=false`,
  one tool result compacted to an observation store (`31394 → 26788` chars).

Independent checks **before** the model edited anything:

- `tsc -b` failed: `Task` is a type and must be imported with `import type`
  under `verbatimModuleSyntax`.
- Browser: `SyntaxError: The requested module '/src/utils/tasks.ts' does not provide an export named 'Task'`. Empty body, no columns.

The **local model** then edited `TaskBoard.tsx`, `TaskCard.tsx`, and
`tasks.ts` (builder did not hand-edit those files). It switched the value
import to `import { type Task, ... }`. The app **mounts**.

Independent checks **after** that recovery (preview `http://127.0.0.1:4554/`):

- Page report: headings `Task Board / To do / In progress / Done`. No
  console errors. `localStorage.tasks` is `[]`.
- Playwright request 1: **1 passed** (columns), **5 failed**. Adding a
  task does not make a card appear (edit/move/search/refresh/phone/blank-
  title checks never get a visible task). `npm run build` still fails on
  unused locals/imports (`noUnusedLocals`), not on the old `Task` export.
- Observed remaining application bug (model-owned, not patched here):
  `addNewTask` calls `addTask(...)` then clears the form and never
  `setTasks(...)`. `deleteTask` / `updateTask` in the component still
  shadow the imported helpers and recurse.

Request 2 (due dates / Overdue) was **not run**.

Builder interventions that were not TaskBoard edits: continued the same
attempt after a false “transport” classification of aborted-but-readable
probes; restarted OpenCode after diagnosing a hung SSE that lacked
`[DONE]` / connection close.

## What this is not

This is not a finished general coder. A mounted three-column board is not
a working task board if created tasks never appear. **Qwen3.5-9B Q4_K_M
did not complete request 1 on this recovery.** Dock app remains 0.3.0.
