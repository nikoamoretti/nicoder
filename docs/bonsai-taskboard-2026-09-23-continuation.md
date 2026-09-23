# Task-board continuation — 2026-09-23

Protected continuation from `ep-20260922-081807-faf7` on existing
`task-board-3`. No new project. Keep was not used. Due dates were not run.

## Result

- The blocking `tsc` errors were **not** fixed.
- The original folder is unchanged
  (`src/App.tsx` sha256 `a127a2f35d4ee5b0fcccbc63e313621aecff4f65485171151008106c3dd71e97`).
- Live/candidate hashes still match faf7. This request wrote no files.

## Live continuation

- Request: `ep-20260923-005611-9efd`
- Path: `AppRuntime.start_request(..., continue_from=ep-20260922-081807-faf7, generation_limit=6)`
- Helper: version `0.4.0`, `BUILD_ID=20260922-repair-context` in
  `~/.local/share/local-coder-venv`. The Mac app binary was not rebuilt
  (Local Coder.app 0.4.0, binary dated 2026-09-18).
- Model: `Ternary-Bonsai-2-27B-PQ2_0`, owned llama-server pid 6018,
  `n_ctx=8192`, `max_tokens=3072`. Context was not raised.

### Timing (this request only)

| Step | Value |
|---|---|
| Wall | 83.64 s |
| Check / `npm run build` | 11.184 s |
| Token accounting | 0.185 s |
| Prompt processing | 56624 ms (`prompt_tokens=3369`) |
| Output generation | 5617 ms (`completion_tokens=72`, `finish_reason=stop`) |
| File apply | not applied |
| Browser-check (in request) | preview modules loaded; `tsc` still failed |
| Generations used | 1 of 6 |

llama-server RSS before the request was about 1–2 MB after ~4 days idle.
The 56 s prompt time is recorded separately from decode. Unavailable
fields stay unavailable (`predicted_per_second` was not in the reply).

### What the model received

The first generation was a repair prompt, not a fresh “rewrite App.tsx”
implement. The check was `build_failed` from `tsc -b && vite build`,
including `App.tsx:291` `COLUMN_LABELS[next]` and `TaskItem.tsx:75`
optional assignment. Preview of `App.tsx` itself returned HTTP 200.

### What the model returned

`last_reply.txt` (282 bytes, `id=chatcmpl-8lqNkC3SQGr0dumwtR69N2WhvG3M3Fae`):

```
I'll read the relevant files to understand the current state and fix the errors.

<tool_call>
<function=read>
src/types.ts
</function>
</tool_call>
<tool_call>
<function=read>
src/TaskItem.tsx
</function>
</tool_call>
<tool_call>
<function=read>
src/App.tsx
</function>
</tool_call>
```

Not truncated. No write or hash-edit fences. The apply path treated this
as “no file blocks” and stopped, leaving 5 generations unused. That stop
was integration, not a second failed repair.

## Current board behavior (browser, candidate preview)

Preview served
`.../requests/ep-20260923-005611-9efd/candidate` at `http://127.0.0.1:4837/`.
The empty board mounted (search, three columns, add form). Adding
“First continuation task” cleared `#root` with no Vite overlay. That
matches `COLUMN_LABELS[next]` being evaluated outside the click handler.
`saveTasks` is still defined and never called.

| Behavior | Result |
|---|---|
| Empty board renders | Yes |
| Add a task | Crashes the page (blank `#root`) |
| Edit details | Untested after the crash |
| Move columns | Untested after the crash |
| Search | Untested after the crash |
| Refresh persistence | Untested; `saveTasks` is unused in current source |

## Integration changes in this commit

- Keep structured repair state across model reads.
- Show the cited file and a window around the failing line.
- Separate repair instructions from implement instructions.
- Record `finish_reason`, usage, timing, and request id. Do not apply
  truncated output.
- Continue from an existing candidate without restarting the user folder.
- Treat foreign `<function=read>` XML as a bounded read, and retry a
  no-file repair reply instead of stopping the loop.

These last two landed after the live reply was already recorded. This
continuation was not restarted.

## Earlier attempts (preserved)

1. `ep-20260922-073937-604f`
2. `ep-20260922-080239-51ee`
3. `ep-20260922-081807-faf7`
