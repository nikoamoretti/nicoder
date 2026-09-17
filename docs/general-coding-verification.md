# Local Coder general coding — verification (in progress)

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

The builder did not patch the generated app.

## Request 2

Not run. Creation failed the behavior checks.

## GUI package

Mac UI now has New Project, preview pane, Stop, Keep/Discard, and a follow-up
box. Packaging the 0.4.0 `.app` still requires a working Swift toolchain
(Xcode license was unsigned on this machine at the time of this note).

## Privacy / isolation notes

- OpenCode `HOME` is app-owned. User OpenCode config still exists and was not
  overwritten.
- Cloud provider env keys are stripped from the OpenCode process.
- `webfetch` / `websearch` / `external_directory` / `task` are denied.
- OpenCode still emits many builtin `plugin.added` events and a
  `catalog.updated` event. Those are recorded; they are not treated as proof of
  cloud inference, and they are also not treated as fully offline.
- llama-server is bound to `127.0.0.1`. Its log still warns that CORS defaults
  are permissive when no API key is set.
- Candidate snapshots exclude `node_modules`, `.env`, and secret globs.
  Uncommitted user source is included.

## Next required approval

Try one larger local model: community Q4_K_M of post-trained Qwen3.5-9B
(`bartowski/Qwen_Qwen3.5-9B-GGUF`, file `Qwen_Qwen3.5-9B-Q4_K_M.gguf`,
**6.17 GB**, SHA-256 listed by the host as
`d784ce9eda1a5a7b51e8f705a9e6310844bf4f173654d115823c775fdea56d43`).
Destination: `~/.cache/local-coder/models/`. Purpose: the same two-request
task-board demonstration after 4B produced a non-working board.

## What this is not

This is not a finished general coder. Offline unit tests passing is not the
acceptance demonstration. The 4B create run is a real local-model attempt and
a real capability miss.
