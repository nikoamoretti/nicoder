# Baseline Collection

## Goal

Collect enough live, eligible observations to qualify a baseline cohort for
each probe version and endpoint, so that later change detection has a
comparison-safe reference.

## Safe collection command

```bash
python -m endpoint_ledger.cli run-baseline \
  --provider openai \
  --endpoint gpt-4o-mini \
  --suite smoke \
  --repeats 5 \
  --cost-ceiling 1.00 \
  --live
```

What `run-baseline` does:

1. **Uses only baseline-eligible probe versions.** Probe versions marked
   baseline-ineligible (measurement defects) are excluded from the matrix.
2. **Prints every cohort fingerprint** it will collect into, before execution.
3. **Shows current qualification progress** for the live cohorts.
4. **Requires `--live`** for live providers and **refuses when the API key is
   unavailable** in the environment. Secrets come only from the process
   environment — never from files.
5. **Preserves accurate suite status** if individual observations fail: the run
   continues, failures are persisted, and the status reflects reality
   (`completed` vs `completed_with_errors` vs `failed`).

Dry-run mode (no API calls, nothing persisted):

```bash
python -m endpoint_ledger.cli run-baseline --provider openai \
  --endpoint gpt-4o-mini --suite smoke --repeats 5 --cost-ceiling 1.00 --live --dry-run
```

## Recommended cadence (MVP policy)

> Five repeats per probe, twice per day, for seven days.

- **Repeats:** 5 per probe per run → 30 primary observations per probe per run
  (6 probes × 5).
- **Runs:** 2 per day × 7 days = 14 runs.
- **Per-probe totals:** ~70 primary observations across ≥ 10 distinct runs and
  ≥ 5 distinct UTC dates — comfortably above the MVP qualification policy
  (7 calendar days, 10 runs, 50 observations, 5 dates).
- **Cost:** the smoke suite at gpt-4o-mini pricing costs well under $0.01 per
  run; the $1.00 ceiling leaves a large margin and the pre-run estimate is
  checked before every live run.

This cadence is a **proposed MVP baseline policy**, not a universal statistical
guarantee. It is calibrated for the first longitudinal measurement of this
exact suite on this exact endpoint; a different model, suite, or deployment
surface may need a different policy.

## Qualification progress

```bash
python -m endpoint_ledger.cli baseline-status --provider openai
```

Example:

```
Baseline status: collecting
Date coverage: 1 of 7 days
Eligible runs: 1 of 10
Eligible observations for probe: 3 of 50
Unresolved measurement issues: 1
```

A cohort becomes `qualified` only when every requirement is met **and** no
unresolved measurement defect affects the primary metric. A provider-reported
model change for the same requested alias starts a **new** cohort and records a
model-resolution event — the old cohort is never silently combined with it.

## What qualifies an observation

An observation enters a cohort when:

- its suite is `live` (never mock, synthetic, demo, or test data);
- the provider request completed without a transport/account error;
- its probe version is baseline-eligible;
- its primary outcome can be determined (explicit primary score, or a
  documented derivation from stored diagnostics for legacy observations);
- no unresolved measurement defect is adjudicated on its primary score.

## What never goes into a baseline

- Mock, synthetic, demo, and test runs.
- Failed provider requests (e.g. the `insufficient_quota` incident in run 5) —
  they are preserved as operational evidence but excluded from behavioral
  statistics.
- Observations from probe versions marked baseline-ineligible.
- Observations whose provider-reported model differs from the cohort's.

## Phase 1.2 evidence lifecycle

Every live run follows the safe lifecycle:

1. **Preflight** (`baseline-preflight`) — refuses unsafe collection: ineligible
   probes, missing primary metrics, unresolved defects, no recent verified
   backup while live evidence exists, unavailable export destination, missing
   API key, or unbounded cost.
2. **Open run** → execute → persist every observation (partial failures
   preserved) → accurate terminal status.
3. **Seal** — live evidence becomes append-only at the database level.
4. **Export + verify** — a hashed portable bundle is written and verified;
   the manifest hash is stored on the run and in `run_artifacts`.
5. **`evidence_state=exported`** — or `export_failed` with the sealed database
   evidence preserved and a prominent error.
6. **Cohort refresh** — the run's observations enter their cohort; progress is
   printed.

A **qualification smoke** run (`--qualification-smoke`, one repeat per probe)
creates real sealed+exported evidence but does not claim qualification; it must
be manually audited and approved with `approve-qualification-run` before normal
collection continues.

Backup rhythm during collection: `make db-backup` + `make db-verify-backup` at
the start of each collection day (destructive maintenance refuses without a
verified backup younger than `ENDPOINT_LEDGER_BACKUP_MAX_AGE_HOURS`, default
24h).
