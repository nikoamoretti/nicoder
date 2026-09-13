# Pilot report pilot-01

Suite: pilot-20 | profile: llama | splits: dev, held-out
Started: 2026-09-13T05:27:22+00:00 | finished: 2026-09-13T05:44:17+00:00
Backend available: True

## Aggregate (nothing dropped silently)

- Scheduled: 20 | attempted: 20 | completed: 20
- Solved: 0 (of completed: 0.0; of attempted: 0.0)
- By status: {'blocked': 20}
- By category: {'blocked-budget': 12, 'blocked-no-progress': 8}
- Mean generations (completed): 6.7
- Mean recall@1/@3 (gold labels only): 0.85/1.0
- Episodes with regression: 0 | with recovery: 0
- Blocked with useful handoff: 20/20

## Per-ticket outcomes

| ticket | split | status | category | gens | patch? | recall@1 |
| --- | --- | --- | --- | --- | --- | --- |
| fam-demo-calc | dev | blocked | blocked-budget | 8 | False | 0.0 |
| fam-localization | dev | blocked | blocked-budget | 8 | False | 1.0 |
| fam-multi-hunk | dev | blocked | blocked-no-progress | 7 | False | 1.0 |
| fam-recovery | dev | blocked | blocked-no-progress | 4 | False | 1.0 |
| gen-arith-op-a | dev | blocked | blocked-budget | 8 | False | 1.0 |
| gen-arith-op-b | dev | blocked | blocked-budget | 8 | False | 1.0 |
| gen-early-return-a | held-out | blocked | blocked-budget | 8 | False | 1.0 |
| gen-early-return-b | held-out | blocked | blocked-no-progress | 4 | False | 1.0 |
| gen-inverted-condition-a | held-out | blocked | blocked-no-progress | 7 | False | 1.0 |
| gen-inverted-condition-b | held-out | blocked | blocked-budget | 8 | False | 1.0 |
| gen-missing-key-a | dev | blocked | blocked-budget | 8 | False | 1.0 |
| gen-missing-key-b | dev | blocked | blocked-no-progress | 4 | False | 1.0 |
| gen-off-by-one-a | dev | blocked | blocked-budget | 8 | False | 1.0 |
| gen-off-by-one-b | dev | blocked | blocked-no-progress | 3 | False | 1.0 |
| gen-str-join-a | held-out | blocked | blocked-budget | 8 | False | 1.0 |
| gen-str-join-b | held-out | blocked | blocked-no-progress | 4 | False | 1.0 |
| gen-swapped-args-a | dev | blocked | blocked-budget | 8 | False | 1.0 |
| gen-swapped-args-b | dev | blocked | blocked-budget | 8 | False | 1.0 |
| gen-wrong-constant-a | held-out | blocked | blocked-budget | 8 | False | 0.0 |
| gen-wrong-constant-b | held-out | blocked | blocked-no-progress | 5 | False | 0.0 |

_Generated fixtures test engineering correctness, not representative real-world coding performance._
