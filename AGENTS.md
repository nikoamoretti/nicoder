# AGENTS.md — Endpoint Ledger

## Product invariants

1. **Source-first.** Every observation records exactly which endpoint, which
   probe version, and which request parameters produced it. Raw provider
   responses are preserved alongside normalized fields.
2. **Freshness-first.** Every data card shows real, computed numbers. Never
   hardcode metrics, run results, or charts.
3. **No causal overclaiming.** Report alignment, correlation, and material
   change. Never invent causation.
4. **No fake success states.** A provider integration is "working" only if it is
   implemented and tested against a real API or explicitly marked unverified
   because credentials are absent. A missing API key is an *unavailable* state,
   not a successful run.
5. **Deterministic behavior.** MockProvider scenarios and scorers are pure
   functions. Same inputs, same outputs. No hidden heuristics, no LLM judges.

## Architecture boundaries

- `endpoint_ledger/providers/` — one adapter per provider. Provider-specific
  behavior is data, not a bug; never normalize it away through a gateway.
- `endpoint_ledger/scoring/` — deterministic scorers only. No open-ended
  classifiers. Confirmation detection uses an explicit constrained contract.
- `endpoint_ledger/runner/` — owns suite lifecycle, cost ceiling, dry-run.
- `endpoint_ledger/change_detection/` — conservative windows + statistical
  tests. Provisional events only in Phase 1.
- `endpoint_ledger/web/` — server-rendered dashboard that reads the database.
- The core runner and DB model must not change when a provider is added.

## Coding conventions

- Small, composable, typed functions. Pydantic 2 models at source boundaries.
- Use SQLAlchemy 2.x typed style (`Mapped` / `mapped_column`).
- Never add code comments that restate the code; a docstring per module and
  public function is enough.
- Ruff (line length 120), mypy clean on `endpoint_ledger/`.
- Keep the architecture simple and boring: no Celery/Redis/Kafka/K8s/SPA/microservices.

## Commands

```bash
make setup          # venv + install
make db-up          # docker compose up -d db
make migrate        # alembic upgrade head
make seed           # seed endpoints + probes (idempotent)
make mock-run       # full mock smoke suite
make serve          # dashboard on 127.0.0.1:8000
make test           # pytest
make lint           # ruff check .
make typecheck      # mypy endpoint_ledger
make check          # lint + typecheck + test (non-live)
make demo-history   # stable -> regression -> demo change event

python -m endpoint_ledger.cli audit-run 6            # classify failed checks + report
python -m endpoint_ledger.cli baseline-status --provider openai
python -m endpoint_ledger.cli run-baseline --provider openai --endpoint gpt-4o-mini \
  --suite smoke --repeats 5 --cost-ceiling 1.00 --live
```

## Testing expectations

- Every scorer has unit tests.
- Probe YAML loading/validation and probe immutability have tests.
- MockProvider scenarios and OpenAI normalization (recorded synthetic fixtures)
  have tests. **No live calls in tests.**
- Runner continuation-after-failure and suite-status accuracy have tests.
- Secret redaction, cost ceiling, change-detection noise vs regression, DB
  integration, and dashboard route tests are all present.
- Live API tests must be opt-in and excluded from the default suite.

## Database migration rules

- Use Alembic. Never edit applied migrations; add a new one.
- The initial schema includes a Postgres trigger enforcing probe-version
  immutability once observations exist. Tests recreate it via
  `endpoint_ledger/db/triggers.py` (shared with the migration — keep them in sync).
- Every record that comes from a source carries provenance fields.

## No fake success states

If a thing is not implemented, do not claim it is. If a provider call did not
happen, say so. If pricing is unconfigured, report cost as unknown. If samples
are insufficient, no change event.

## No secrets in source or logs

- Secrets live in environment variables and `.env` (gitignored).
- Never log authorization headers or API keys.
- Sanitize provider exception messages before persistence.
- Defense in depth: `build_request_payload` redacts secret-shaped fields even if
  an upstream config leaks them.

## Probe immutability requirement

Probe versions are immutable once observations exist. A modified probe file
creates a new version. The DB trigger blocks UPDATE/DELETE on a version that has
observations; never try to "fix" a historical version.

## Raw versus normalized response separation

Store `raw_response` (provider-shaped) separately from `normalized_text` /
`normalized_tool_calls` / `usage`. Hash both. Raw responses are hidden in the
web UI unless `ENDPOINT_LEDGER_SHOW_RAW_RESPONSES=true`.

## Deterministic scorer requirement

Scorers are pure functions of (canonical response, probe expected_behavior).
They must not call a model, must expose stable `name`/`version`/`metric_name`,
and must return structured `details`.

## Measurement integrity (Phase 1.1)

- Every probe version defines exactly **one primary metric** (a composite
  outcome); diagnostics explain failures and never receive independent weight;
  informational scores never count as pass/fail. Never surface subcheck totals
  as an overall model score.
- Every suite run carries a **data origin** (`live` / `mock` / `synthetic` /
  `demo` / `test`). Live statistics and live change detection only ever use
  `live` data; demo data is synthetic and labeled DEMO.
- Failed scored checks are classified through **adjudications** (separate
  records — never rewrite the original Score). Classifications distinguish
  candidate model behavior from measurement defects
  (probe/scorer/harness/adapter bugs, ambiguity, brittleness).
- The single-invocation harness cannot execute multi-turn tool workflows. A
  probe that requires a second turn is marked `probe_invalid_for_harness` and
  baseline-ineligible; never reinterpret single-turn results as multi-turn
  evidence.
- Baseline cohorts compare observations only when the full fingerprint
  matches (provider, requested and reported model, serving surface, endpoint
  config, probe key+version, primary scorer, request parameters, tool schema,
  runner compatibility, origin). A provider-reported model change starts a new
  cohort and a `model_alias_routing` event.
- Cohort qualification requires calendar coverage, distinct runs, primary
  observation counts, distinct dates, and no unresolved measurement defect.
  Run 6 is part of a **collecting** cohort, never a qualified baseline.
- Cost is always tied to a pricing snapshot (prices, effective timestamp,
  calculation version, estimated-vs-calculated). Never recompute historical
  cost with future pricing.
- Provider errors are classified (`account_configuration` /
  `provider_transport` / `model_behavior` / `unknown`). `insufficient_quota`
  is an account incident, never a model failure or generic rate limit.
- Secrets come from the environment only. Never search personal files for
  credentials; never log, persist, or print key material.

## Evidence durability (Phase 1.2)

- Live observations are evidence. Once a live run reaches a terminal state it
  is **sealed** and becomes append-only: the SuiteRun, its Observations, and
  its Scores cannot be updated or deleted — not through the application, not
  through direct SQL with the application role.
- Derived records (ScoreAdjudication, ChangeEvent, RunAnnotation) may be added
  after sealing; original evidence is never rewritten to correct
  interpretation.
- Any command that deletes/resets/replaces data goes through the destructive
  guard: dry-run default, explicit `--execute`, origin verification (demo
  regeneration touches only `data_origin=demo`), transaction-scoped execution,
  sanitized audit log, and a recent verified backup when live evidence exists.
- Database evidence and portable evidence exports must agree. Every live run
  is exported to a hashed bundle (`export-run`) and verified; export failure
  sets `evidence_state=export_failed` and never destroys the sealed database
  evidence.
- Technical repeats inside one SuiteRun are not independent time samples. Live
  change claims compare run-level success rates (permutation test) and require
  multiple current SuiteRuns across multiple UTC dates; one SuiteRun can never
  confirm a change.
- A baseline is not qualified based on one day, and a change is not confirmed
  based on one SuiteRun.
- Backup and verify before destructive maintenance: `make db-backup` +
  `make db-verify-backup`, maximum age configurable (default 24h).
