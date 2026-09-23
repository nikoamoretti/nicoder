# Task-board read continuation — 2026-09-23

Linked continuation of `task-board-3` from `ep-20260923-005611-9efd`
(chain: `faf7` → `9efd` → `7d0e` → `2ebb`). No new project. Keep was not
used. Due dates were not started.

## Result

- Bonsai did not get past reading. It proposed no edit, so nothing was
  applied and the build still fails with the same 17 `tsc` errors.
- Adding a task still crashes the page on the correct candidate preview:
  `#root` empties with `Uncaught ReferenceError: next is not defined`.
  Edit, move, search, and refresh were not reachable.
- The original folder is unchanged. The candidate source is byte-identical
  to `faf7` and `9efd`.
- 5 of 5 authorized generations used, 254 s of model-continuation time
  (18-minute limit). Runs went through `AppRuntime.start_request`, not the
  Mac window.

## First unsuccessful transition

| Request | Generations | What happened |
|---|---|---|
| `ep-20260923-030257-7d0e` | 2 | Response parsing. Bonsai asked for permitted reads as `<function=read src/App.tsx` (path inside the opening tag). The committed parser did not recognize that shape, and the format error said "tool_call XML is only understood for reads", so it repeated the same reply. Stopped as no progress. |
| `ep-20260923-030726-2ebb` | 3 | Reading. Every read was parsed and served (15–100 new lines each), and the status line said so. Each reply asked again for "the full source files" (App.tsx is 352 lines) and proposed no edit before the generation limit. |

The 9efd prompt was rebuilt from its saved repair state and counted with
the server tokenizer (3,354 tokens vs 3,369 reported). It did not contain
the source for `App.tsx:291` (`COLUMN_LABELS[next]`) or `saveTasks`, so
that run's read request was reasonable. The new first prompt for the same
state carries every error line in 2,536 tokens.

## Integration changes

- Reads in repair mode are intermediate steps. Bounded 100-line chunks are
  merged with the error windows; the next read of a file continues where
  the last one stopped; identical repeat reads count as no progress.
- One budgeted source view per file, sized from `n_ctx − max_tokens −
  margin`. Error lines are never dropped. Newly requested lines outrank
  context; space is shared round-robin across requested files.
- Retry history comes from the actual outcome: applied (with changed
  files), read, repeat read, rejected (with the reason), truncated, or no
  action. "Not applied" is said only when true.
- Edits: several edits to one file apply in sequence; ambiguous or
  non-consecutive hash spans are rejected with a reason; `>` markers and
  pasted `hash|text` are tolerated.
- Reads and edits refuse paths that escape the candidate, including through
  symlinks. XML is recognized only for `read`.
- The build script cannot change during repair. Unchanged writes are not
  counted as applied. Oversized prompts shrink without spending a
  generation. The wall budget reserves time for one more call plus check.
- Every prompt and raw reply is stored under the request's `turns/`.

## Timing (actual calls)

| Request.turn | Outcome | Prompt tokens (cached) | Prompt ms (tok/s) | Output tokens | Output ms (tok/s) |
|---|---|---|---|---|---|
| 7d0e.1 | no action | 2591 (34) | 36872 (69.3) | 87 | 6127 (14.0) |
| 7d0e.2 | no action | 2719 (34) | 33064 (81.2) | 83 | 5932 (13.8) |
| 2ebb.1 | read | 2591 (2587) | 7424 (4 new) | 51 | 3506 (14.3) |
| 2ebb.2 | read | 3893 (34) | 43643 (88.4) | 89 | 6231 (14.1) |
| 2ebb.3 | read | 3890 (34) | 43262 (89.1) | 84 | 5862 (14.2) |

Checks (build + preview) took 8.0 s and 6.2 s. Token accounting took
0.04–0.47 s. No file apply or post-edit check happened.

Uncached prompt processing was 69–89 tok/s in back-to-back calls, so this
pass does not support attributing the earlier 56 s prompt to idle paging.
It also does not establish a cause. During each run, free memory fell to
12% and swap use grew 2–3.4 GB (see `summary.json`).

## Helper

`~/.local/share/local-coder-venv`, version 0.4.0,
`BUILD_ID=20260923-read-continuation-2`. Every imported module matches
the checkout byte-for-byte (`helper_identity.json`). The `7d0e` run used
`20260923-read-continuation`, which lacked the inline-path read form. The
Local Coder.app binary was not rebuilt.

## Evidence

`docs/evidence/bonsai-taskboard-2026-09-23-read-cont/`: exact prompts and
replies, the reconstructed 9efd prompt, `summary.json`, and
`helper_identity.json`. Candidate preview: `http://127.0.0.1:4837/`
serving `requests/ep-20260923-030726-2ebb/candidate`.
