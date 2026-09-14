# compare-03 analysis: corrected v3 catalogue and bounded transport (dev evidence)

Code frozen at `49183e9` (report `code_sha`). Catalogue `span-catalogue-v2`,
grammar `gbnf-action-v2b`/`gbnf-action-v3b`, transport `sse-transport-v2`.
Model `Qwen3.5-4B-Q4_K_M`, llama.cpp b10809, `--reasoning off`,
final-channel-only throughout. Four curated DEV tasks; 8 cells; 2 probes
(one per profile, both recorded, both succeeded). Generated fixtures:
development evidence, not a general solve rate.

## Profiles

- **D1** v2 exact-text replacement + mandatory post-edit verification.
- **D2** corrected v3 span catalogue + mandatory post-edit verification.

Same transport hardening, sampling, retrieval, task/grader versions, and
budgets for both. Order alternated per task (`D1,D2` / `D2,D1`).

## Cell results

| task | profile | outcome | stop reason | gens | applied | auto checks | improved | rejects |
|---|---|---|---|---|---|---|---|---|
| fam-demo-calc | D1 | **solved** | solved | 5 | 3 | pass, fail, pass | add tests | – |
| fam-demo-calc | D2 | **solved** | solved | 3 | 1 | pass | add tests | – |
| fam-localization | D1 | blocked | no validation budget for auto-check | 6 | 2 | pass, pass | refresh test | – |
| fam-localization | D2 | **solved** | solved | 3 | 1 | pass | refresh test | – |
| fam-multi-hunk | D1 | blocked | no-progress | 5 | 0 | – | – | old_text_missing |
| fam-multi-hunk | D2 | blocked | no validation budget for auto-check | 6 | 2 | fail, fail | line_total | – |
| fam-recovery | D1 | **solved** | solved | 4 | 1 | pass | increment tests | – |
| fam-recovery | D2 | **solved** | solved | 3 | 1 | pass | increment tests | – |

Totals: D1 2/4 solved, 6 applied edits, all 6 edit checks executed
(4 pass, 1 fail then corrected, 1 pass); D2 3/4 solved, 5 applied edits,
all 5 checked (3 pass, 2 fail).

## The two corrections, evidenced

**Span granularity.** D2 offered 3–21 editable spans per request, including
children: e.g. fam-recovery offered all three methods plus every assignment
and return inside each, each labelled with `kind` and `enclosing`
(`Counter.increment`, `Counter.reset`). The recovery solve used `S5`
(`assign` inside `Counter.increment`) — a child statement, not the class.
Children precede parents in the catalogue, and budget trimming pops parents
first, so small edit units survive. D2 solved three tasks in 3 generations
each; compare-02's D2 (parent-only catalogue) required a record the model
could not target: its three `syntax_error` rejections there were class-span
selections with method-fragment text. In compare-03 no such failure occurs;
the only D2 block is a budget stop after verified partial progress.

**Editability before ID assignment.** Read-only paths are omitted from the
catalogue with a recorded reason, before any ID is assigned. Across D2,
gold files received 3–21 spans each; `tests/`, `conftest.py`, and
`local-coder.toml` received none (recorded `read_only_omitted` per cell;
`tests/test_*` remained visible as retrieval excerpts). The compare-02
failure mode where the model spent an attempt on a test-file span
(`forbidden_path`) cannot recur: no such target is advertised, and
apply-time authorization remains as defense in depth.

**Transport termination.** Both probes now succeed, including the v3
`span-shape` probe that previously ended in a 128 KiB transport breach: the
whitespace-bounded grammar prevents the degenerate run. One real transport
event occurred and was handled honestly: fam-multi-hunk D2 hit the
131072-byte transport cap after 519 events because the model streamed a
runaway action (decoded content only 1789 bytes; finish reason never
arrived). Diagnostics recorded the exact limit, byte/event counts, decoded
sizes, finish/terminal state, and a bounded prefix; the slot was consumed,
nothing partial was executed, and the episode continued and later stopped
honestly. v2b/v3b grammar identities, catalogue version, and transport
policy/limits are recorded in every turn's `profile.json`.

## Coverage, measured separately from code generation

D2 `span_file_offered` = 4/4 (gold file in the catalogue) and
`useful_unit_offered` (offline gold-line overlap) = 3/4.

- fam-multi-hunk D2: `line_total` was offered and fixed (target improved
  after two verified edits), but the second gold change (`grand_total`,
  module line 27) was outside the FIND excerpt; it was never offered, so
  that block is **retrieval/span-coverage**, not targeting or code-writing.
- fam-recovery D2: `useful_unit_offered` is False because the gold diff
  edits the module constant `INCREMENT` (line 4), which the excerpt did not
  cover; the model instead fixed `self.value += INCREMENT` inside the
  `increment` method via a child span (`S5`) — a different, correct edit
  that passed every test. Gold-line overlap under-reports solvability here.

## Failure classification

- Targeting: none in D2. (compare-02 had class-span/method-fragment
  mismatch; the child catalogue removes it.)
- Code writing: fam-multi-hunk D2 produced two wrong edits, both caught by
  real failing checks (grand_total never reached because its unit was not
  offered).
- Identity/stale/hash: none; no patch rejections in any D2 cell.
- Retrieval/coverage: fam-multi-hunk D2 (second unit not in focus).
- Workflow: D1 fam-localization improved its target with both edits verified
  passing, then attempted another patch without reserved validation room and
  stopped honestly with `no validation budget for mandatory post-edit check`.
- Transport: one runaway stream bounded and recorded; no partial execution.
- Budget: no episode exceeded any cap.

## Memory (measured, not qualified)

Controller RSS 58–61 MB; local server RSS 414–566 MB samples on a 16 GB
machine with 1.7–1.9 GB host-available; worker image 149 MB on disk (not
runtime RAM). No 16 GB suitability claim is made from development fixtures.

## Readiness

- Engineering gates: permissions, span identity, transactional publication,
  check integrity, budget reservation, bounded transport, and persistence
  all pass with dedicated tests and live evidence.
- Model capability: D2 3/4 dev solves; D1 2/4; remaining blocks are
  coverage/budget/stopping behavior, not interface mechanics.
- Recommendation: freeze D2 (corrected v3 + mandatory verification) as the
  default experimental profile, with D1 retained as the explicit fallback.
  Prompt 5 may package an experimental developer v1; it must state that
  c3 evidence is four curated dev fixtures and does not qualify 16 GB
  hardware or claim general coding performance.