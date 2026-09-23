# Task-board coherent-input continuation — 2026-09-23

Protected continuation of `task-board-3` from `ep-20260923-030726-2ebb`
(chain: `faf7` → `9efd` → `7d0e` → `2ebb` → `8ce5`). No new project.
Keep was not used. Due dates were not started.

## Result

- Bonsai **did propose corrections**, and two of them **applied**.
- Disappeared: `TS2614` (TaskBoard named `TaskItem` import) and `TS2779`
  (optional assignment on `dataTransfer`).
- New: `TS2322` on `TaskBoard.tsx:214` (`(id: string) => void` vs `() => void`)
  after the default import actually wired `TaskItem` props.
- Remaining: `App.tsx:291` `next` (`TS2304`/`TS7053`), `useMemo` missing in
  TaskBoard, type-only import errors, unused names, implicit `any`.
- `npm run build` still fails. Add / edit / move / search / persistence were
  **not tested**. The `next` crash site is unchanged in `App.tsx`.
- Original folder unchanged (`App.tsx` sha256 `a127a2f35d4ee5b0…`).
- Helper `0.4.0` / `BUILD_ID=20260923-coherent-input`, all imported modules
  match the checkout. App binary was not rebuilt.

This does **not** make Ternary-Bonsai-2-27B suitable as the product model.
Two small, well-scoped repairs landed. The next coherent target (`next` on
`App.tsx:291`) was understood in prose, then the model spent the output
budget rambling and was truncated, so the edit was not applied.

## Live run

| | |
|---|---|
| Episode | `ep-20260923-055219-8ce5` |
| Continue from | `ep-20260923-030726-2ebb` |
| Path | `AppRuntime.start_request` (not the Mac window) |
| Model | Ternary-Bonsai-2-27B-PQ2_0, n_ctx=8192, max_tokens=3072, pid 6018 |
| Generations | 4 of 6 |
| Elapsed | 720 s (wall). Ledger remaining 0.07 s |
| Stop | `no_progress` after two truncated replies; waiter also hit the 12-minute cap |

| Turn | Outcome | Prompt tok (cached) | Prompt ms (tok/s) | Output tok | Output ms (tok/s) |
|---|---|---|---|---|---|
| 1 | applied `src/TaskBoard.tsx` | 1906 (34) | 28094 (66.6) | 78 | 5373 (14.3) |
| 2 | applied `src/TaskItem.tsx` | 1869 (34) | 29882 (61.4) | 90 | 7542 (11.8) |
| 3 | truncated, not applied | 1624 (34) | 30700 (51.8) | 3072 | 247309 (12.4) |
| 4 | truncated, not applied | 1617 (34) | 24078 (65.7) | 3072 | 286299 (10.7) |

Checks: 7.3 s, 6.3 s, 9.9 s. Token accounting 0.14–0.28 s.

Memory: swap used 22078M → 25477M; llama RSS 4416 kB → 13648 kB. Free
percentage reported 39% before and 56% after (not a claim about paging).

## Active target before generation

Preflight `check_candidate` reproduced the same 17 `tsc` errors. Selected
target (deterministic: import/export before assignment before `cannot find
name`):

`src/TaskBoard.tsx(3,10): error TS2614: Module '"./TaskItem"' has no exported member 'TaskItem'.`

Packet included TaskBoard import lines and TaskItem `export default function
TaskItem`. `src/App.tsx` was listed as not in the request. Prompt 5677 chars,
under the 8192/3072 budget.

## What applied

Turn 1: `import { TaskItem } from "./TaskItem"` → `import TaskItem from "./TaskItem"`.

Turn 2: `e.dataTransfer?.effectAllowed = "move"` → `e.dataTransfer.effectAllowed = "move"`
(indent on that line was dropped; TS2779 left the remaining list).

Turn 3 packet for `App.tsx:291` included the `onClick` handler that declares
`next` **and** the sibling `aria-label` that uses it. The model described the
scope bug correctly, eventually emitted an `edit` fence, then kept writing
until `finish_reason=length`. Truncated replies are not applied.

## First unsuccessful transition

Exact input: `docs/evidence/bonsai-taskboard-2026-09-23-coherent/turns/03-prompt.txt`

Exact response: `docs/evidence/bonsai-taskboard-2026-09-23-coherent/turns/03-reply.txt`

## Integration changes

- Current-request visibility is separate from historical reads. Prompts list
  what they include and omit. "Already shown in full, so not repeated" is gone.
- One active diagnostic group per repair decision. Remaining errors and the
  original request stay in runtime and in the prompt.
- Additional reads fill leftover budget and cannot evict the active packet.
- Repeat reads that add no new lines stop via existing no-progress handling.
  An edit is not required after an arbitrary read count.
- An applied edit drops FileViews for changed files; the next target is
  chosen only after a real check.

## Checks run offline

Full `pytest tests/` passed. Ruff clean on changed files. mypy clean on
changed modules. Full-package mypy still has the same 9 pre-existing errors
in `activity.py` and `chat_log.py`.
