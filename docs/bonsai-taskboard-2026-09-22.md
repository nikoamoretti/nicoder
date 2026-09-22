# Task-board demonstration — 2026-09-22

This is a continuation on existing project `task-board-3`, not a from-scratch
build. Historical Qwen/OpenCode attempts under the same project remain on disk
and do not describe Bonsai 27B.

## Result

- The board does **not** fully work. A three-column layout and add form render
  behind a Vite overlay. Keep was not used. Due dates were not run.
- Original folder `/Users/nico-yardlogix/Local Coder Projects/task-board-3`
  is unchanged (hashes match the pre-request baseline).
- Open **Local Coder.app** (0.4.0, helper `BUILD_ID=20260922-import-graph`)
  and that same `task-board-3` folder. Preview:
  `http://127.0.0.1:4837/` on the disposable candidate.

## Remaining failure

`src/TaskItem.tsx:75` still has `e.dataTransfer?.effectAllowed = "move"`, which
Vite SWC rejects. That overlay covers the page. The live `src/App.tsx` is a
self-contained board (search, three columns, add form) that dump-dom mounted,
but `saveTasks` is unused (refresh would not persist) and `COLUMN_LABELS[next]`
is referenced outside the move click handler.

## Runtime

- Model: `Ternary-Bonsai-2-27B-PQ2_0`, owned llama-server pid 6018,
  `n_ctx=8192`, `max_tokens=3072`. Context was not raised.
- Helper: `~/.local/share/local-coder-venv` (same interpreter the Mac window
  loads). Version label stayed 0.4.0; `BUILD_ID` distinguishes the code.
- Path: `AppRuntime.start_request` (app-service), engine `general`. Not typed
  into SwiftUI. Not OpenCode. Not Qwen.
- Generations: 4 + 3 + ~4 ≈ 11 of 30. Active model time ≈ 43 of 45 minutes.
  Attempt 3 was cancelled at the 16-minute demo remainder.

## Attempts (kept)

1. `ep-20260922-073937-604f` — false `vite_overlay` from `/@vite/client` JS.
2. `ep-20260922-080239-51ee` — real `tsc` errors fed back; stopped after 2
   repairs with ~7 minutes of CREATE wall left (since raised to 8).
3. `ep-20260922-081807-faf7` — TaskItem SWC 500 fed back; demo wall cancelled
   a later generation.

## Integration fixes in this commit

Write → check → repair now re-runs `npm run build` after preview install,
preserves `node_modules` when publishing the candidate, ignores the Vite
client library as an overlay, treats `/@vite/client` HTML and empty `#root`
as not mounted, and fetches the import graph (not every unused file).
