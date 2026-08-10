# Incident — Demo-history data loss (2026-08-09)

## What happened

While verifying Phase 1.1, the `generate-demo-history` CLI command was executed
against the development database. At the time, that command's cleanup step
deleted **every** `SuiteRun` row:

```python
# Pre-fix implementation (destroyed):
for event in session.scalars(select(ChangeEvent)).all():
    session.delete(event)
for run in session.scalars(select(SuiteRun)).all():
    session.delete(run)
```

Because `suite_runs` cascades to `observations` and `observations` cascades to
`scores`, this removed all runs and their evidence, including the two real live
runs:

- **Run 5** — live OpenAI run: 18 observations, all real HTTP 429
  `insufficient_quota` (account-configuration incident).
- **Run 6** — live OpenAI baseline `live-openai-baseline-2026-08-09`: 18
  successful observations, 126 score records (97 passing, 11 failed, 18
  informational latency), actual cost $0.000623.

## Which records were lost

From the development database (`endpoint_ledger`):

- All `SuiteRun` rows (runs 1–8).
- All `Observation` rows (including runs 5 and 6).
- All `Score` rows (including run 6's 126 scores).
- `ScoreAdjudication` rows attached to run 6's scores (cascade).
- Demo and mock runs from earlier phases.

Preserved:

- Probe definitions and versions (v1 + v2), endpoints, migrations.
- The forensic analysis: `docs/audits/run-6-audit.md` documents all 11 failed
  scored checks with observation IDs, hashes, normalized evidence,
  classifications, and baseline-eligibility statements.
- This repository's history of the Phase 1.1 verification steps.

## Root cause

`generate-demo-history` used a global cleanup ("the demo owns its history")
instead of scoping deletion to `data_origin='demo'`. There was no destructive
operation guard: no dry-run default, no origin verification, no backup
requirement, and no audit log. A single command could therefore destroy
unrelated live evidence.

## Why the records are not being fabricated

Runs 5 and 6 are not recreated. Reconstructing them would require inventing
timestamps, latencies, token usage, and 18 raw provider responses that no
longer exist. Invented evidence would violate the project's core invariant:

> Live observations are evidence. A passing provider request is not the same
> as a passing behavioral task, and fabricated records are fake success states.

The audit report documents what was observed and why it failed; it does not
replace the deleted raw observations.

## What evidence survives

- `docs/audits/run-6-audit.md` — full forensic detail for the 11 failed checks.
- The conversation record of the Phase 1.1 verification (aggregate statistics,
  per-probe pass counts, cost, model identifiers).
- Probe versions v1 (immutable) and their definitions.

## Immediate corrective action (already completed)

1. `generate-demo-history` now operates **only** on `data_origin='demo'`
   records (`operations.delete_demo_runs`), verified by a regression test:
   `test_demo_history_never_deletes_live_runs`.
2. The incident is documented here.

## Additional protections introduced in Phase 1.2

1. **Sealed live evidence is database-immutable.** Live runs reaching a
   terminal state are sealed (`evidence_state=sealed`); triggers block UPDATE
   and DELETE of the run, its observations, and its scores — including direct
   SQL with the application role. See `endpoint_ledger/db/sealed_evidence.py`.
2. **Safe destructive-operation framework.** Dry-run by default, explicit
   `--execute`, origin verification, transaction-scoped execution, sanitized
   audit log (`maintenance_operations`), and a recent verified backup
   requirement whenever live evidence exists.
3. **Database backups.** `make db-backup` / `db-list-backups` / `db-restore` /
   `db-verify-backup` with timestamped compressed dumps, verification in a
   temporary database, and configurable maximum backup age.
4. **Portable run evidence bundles.** `export-run` / `verify-run-export` /
   `import-run-export` produce hashed, verifiable, secret-scanned bundles for
   every live run; export failure never destroys the sealed database evidence.
5. **Stale cohorts invalidated.** Live cohorts whose observations were
   destroyed are marked `invalidated` with reason
   `dev_data_loss_incident_2026_08_09`, hidden from the default view, and can
   never qualify or participate in change detection.

## How restoration will be tested

- `test_evidence_durability.py::test_export_import_round_trip_preserves_evidence`
  proves a complete run export can be imported into an empty database with all
  evidence fields and hashes preserved.
- `test_demo_history_never_deletes_live_runs` reproduces the original dangerous
  condition (live + demo runs present) and verifies demo regeneration leaves
  every live record untouched.
- The sealed-evidence tests prove runs 5/6 could never be deleted this way
  again, even with direct SQL.

## Remaining limitations

- The deleted raw observations are unrecoverable; the next live baseline run
  (`run-baseline`) re-establishes live evidence.
- Database-level triggers protect the application role; an administrator who
  can drop triggers or use replication-role bypass can still destroy data.
  Backups are the defense-in-depth for that class of actor.
- `docs/audits/run-6-audit.md` preserves the analysis but is not the raw
  evidence.
