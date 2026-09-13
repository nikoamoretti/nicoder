# compare-02 analysis: span editing and mandatory verification (development evidence)

Code frozen at `49b4e4c` (recorded in report.json `code_sha`). Model
`Qwen3.5-4B-Q4_K_M`, llama.cpp b10809, `--reasoning off`, final-channel-only,
v2/v3 grammars as per profile. Four curated dev tasks, 12 cells, first
outcomes only. Generated fixtures: development evidence, not a general
solve rate.

## Profiles

- **C0** protocol v2 exact-text replace, verification policy off (control).
- **D1** protocol v2 exact-text replace, mandatory post-edit verification.
- **D2** protocol v3 span-ID replace, mandatory post-edit verification.

Order was rotated per task (C0/D1/D2, D1/D2/C0, D2/C0/D1, C0/D1/D2) so no
profile permanently ran first or last.

## Cell results

| task | profile | outcome | block reason | gens | applied | auto checks | verified | rej (non-budget) |
|---|---|---|---|---|---|---|---|---|
| fam-demo-calc | C0 | blocked | no-progress (find) | 8 | 3 | 0 | no | – |
| fam-demo-calc | D1 | **solved** | – | 5 | 3 | 3 (pass, fail, pass) | yes | – |
| fam-demo-calc | D2 | **solved** | – | 8 | 1 | 1 pass | yes | syntax_error |
| fam-localization | C0 | blocked | no-progress | 4 | 0 | 0 | no | – |
| fam-localization | D1 | blocked | no-progress | 4 | 0 | 0 | no | – |
| fam-localization | D2 | blocked | budget | 8 | 0 | 0 | no | syntax_error ×2 |
| fam-multi-hunk | C0 | blocked | budget | 8 | 0 | 0 | no | old_text_missing ×2 |
| fam-multi-hunk | D1 | blocked | budget | 8 | 0 | 0 | no | old_text_missing |
| fam-multi-hunk | D2 | blocked | no-progress | 8 | 1 | 1 fail | yes | forbidden_path, noop_edit |
| fam-recovery | C0 | **solved** | – | 7 | 3 | 0 | n/a (policy off) | – |
| fam-recovery | D1 | **solved** | – | 3 | 1 | 1 pass | yes | – |
| fam-recovery | D2 | blocked | no-progress | 8 | 0 | 0 | no | syntax_error |

Per-profile totals: C0 1/4 solved, 6 applied, 0 auto checks. D1 2/4 solved,
4 applied, 4 auto checks. D2 1/4 solved, 2 applied, 2 auto checks.
Caps: 12 cells, exactly 2 probes (cap respected).

## Probes (task-independent, 2 of max 2)

1. `check-final-channel` — ok. v3 grammar + `--reasoning off` produce a flat
   action in the final content channel.
2. `span-shape` — **failed**: recorded detail is "response exceeded
   131072 byte cap while receiving"; the stream ran to the transport cap
   before closing the action. Recorded, not retried. The stored record does
   not capture the streamed bytes, so no finer cause is claimed.

## Verified behavior (the point of the iteration)

- D1 fam-demo-calc: edit → automatic check (pass) → edit → check (real
  failure evidence) → edit → check (pass) → DONE → final validation; 5
  generations, exactly 5 test invocations (baseline + 3 auto + final).
- D1 fam-recovery: one verified edit, solved in 3 generations.
- D2 fam-demo-calc: one syntax-rejected attempt, then a verified span edit
  that solved the task.
- D2 fam-multi-hunk: a span edit landed and produced a genuine target-test
  improvement (`test_line_total`) before the episode blocked on repetition.
- All 4 D2 cells had the gold file offered in the span catalogue
  (`span_offered_gold: true`), so none of the D2 failures is a
  missing-coverage failure.

## Root-cause evidence for the dominant D2 failure

Three D2 failures are span-granularity mismatches, not missing spans and not
grammar defects. Byte-level reproduction (fam-recovery D2, turn 2):

- Span `S1` = the entire `class Counter` (bytes 71–430).
- The model selected S1 but wrote only the indented `increment` method as
  replacement text; applying it at class level yields
  `unexpected indent (line 6)` and the controller rejects with
  `syntax_error`, restoring exact bytes.

The catalogue offers small compounds whole (under `MAX_SPAN_BYTES`) and does
not also offer their child statements, so the only selectable unit for that
file was the full class. The model wanted a smaller unit.

One further interface leak, quantified across the D2 cells: the catalogue
also offered spans in non-editable paths (`tests/`, `conftest.py`,
`local-coder.toml`). Offered vs editable spans on the first span-bearing
turn: demo-calc 4/1, localization 4/2, multi-hunk 2/1, recovery 4/1. The
controller correctly rejected the attempt in multi-hunk with
`forbidden_path`, but retrieval visibility should not imply span
addressability.

## Remaining failure categories by profile

- C0/D1 (text matching): `old_text_missing` (byte-exact reproduction) plus
  repeated-FIND no-progress. D1's mandatory checks turned 1 extra dev task
  into a verified solve and produced real failure evidence where C0 applied
  unverified edits and stalled.
- D2 (span IDs): unit-granularity mismatch (class-level span selected,
  fragment written), one non-editable span offered, one no-op, plus the
  same retrieval/no-progress loops.
- No regressions and no recoveries occurred in this run: D1/D2 always
  consumed failing evidence and then either corrected or stalled; the
  revert path was exercised only in unit tests.

## Memory (measured, not qualified)

Controller RSS 56–66 MB; local server RSS 1.9–2.4 GB samples on a 16 GB
machine with 1.7–2.0 GB host-available during cells; worker image 149 MB on
disk (not runtime RAM). No 16 GB suitability claim is made from development
fixtures.

## Recommended next iteration (not applied: comparison froze at 49b4e4c)

1. Span catalogue: also offer direct child statements of small compounds
   (budget-bounded), so a single method or line is selectable.
2. Exclude non-editable paths from the span catalogue (retrieval stays).
3. v3 grammar: pin the generation tail (e.g. bound trailing whitespace) to
   avoid the degenerate stream seen in probe 2.