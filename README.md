# Endpoint Ledger

**A source-linked ledger for repeatedly testing live AI model endpoints and
detecting material behavior changes over time.**

Endpoint Ledger is not another general AI benchmark leaderboard. Its core data
object is:

> A specific AI endpoint, through a specific provider, at a specific timestamp,
> under exact request parameters, returned an observable behavior that can be
> scored and compared with prior observations.

The Phase 1 product proves a full vertical slice: run a **mock** provider and an
**OpenAI** provider against six version-controlled probes, score responses with
deterministic scorers, persist every observation to PostgreSQL, run a
conservative change detector, and inspect everything in a private server-rendered
dashboard.

---

## Product purpose

Long-term, Endpoint Ledger detects when externally served AI systems change in
ways that could silently affect production applications — a model that stops
honoring JSON Schema, a tool that starts hallucinating arguments, latency that
doubles, or a newly flaky endpoint.

Phase 1 focuses on four production-relevant behaviors:

1. Structured-output reliability
2. Tool-calling reliability
3. Precise instruction adherence
4. Operational behavior (latency, errors, tokens, cost)

## Architecture

```
endpoint_ledger/
  config.py              environment-driven configuration
  cli.py                 Typer CLI (db upgrade / seed / run-suite / ...)
  db/                    SQLAlchemy 2.x models + sessions
  providers/             ProviderAdapter protocol + MockProvider + OpenAI adapter
  probes/                probe loading/validation + versioning
  scoring/               deterministic scorers (no LLM judges)
  runner/                suite runner + canonical response
  change_detection/      conservative windows + Fisher exact / Mann-Whitney
  web/                   FastAPI + Jinja dashboard (private)
probes/smoke/            the six version-controlled probe definitions (YAML)
probes/fixtures/         recorded synthetic OpenAI fixtures (for tests)
alembic/                 schema migrations
tests/                   unit + integration + dashboard tests
docs/                    product spec and architecture
```

Data flow:

```
probe YAML files ──> Probe/ProbeVersion (immutable) 
endpoint config  ──> ProviderEndpoint
SuiteRunner: endpoint x probe x repeat ──> ProviderAdapter.invoke()
                                          ──> CanonicalProviderResponse
                                          ──> Observation + Scores (PostgreSQL)
ChangeDetector: baseline vs current windows ──> ChangeEvent (provisional)
Dashboard reads PostgreSQL only (no fake metrics)
```

## Prerequisites

- Python 3.12
- PostgreSQL 16 (via Docker Compose, or any local PostgreSQL 14+)
- `uv` (recommended) or `pip` + `venv`

## Exact local setup

```bash
# 1. Create the virtual environment and install the package + dev tools.
make setup

# 2. Start PostgreSQL.
#    Option A (recommended): Docker
make db-up
#    Option B: local PostgreSQL already running on :5432, current user is a superuser
createdb endpoint_ledger
#    In both cases make sure ENDPOINT_LEDGER_DATABASE_URL points at the database
#    (see .env.example). Docker users can copy .env.example to .env unchanged.

# 3. Configure environment.
cp .env.example .env     # edit values; OPENAI_API_KEY stays empty unless you want live runs

# 4. Create the schema.
make migrate

# 5. Seed endpoint configurations and the six probes (idempotent).
make seed
```

## Database migration commands

```bash
python -m endpoint_ledger.cli db upgrade    # apply all migrations
# downgrade one step:
alembic downgrade -1
```

## Seed commands

```bash
python -m endpoint_ledger.cli seed
python -m endpoint_ledger.cli list-endpoints
python -m endpoint_ledger.cli list-probes
```

## Mock run commands

```bash
# Full smoke suite against the mock provider (no live APIs, 3 repeats/probe).
python -m endpoint_ledger.cli run-suite --suite smoke --provider mock

# See the request matrix without calling anything or writing to the database.
python -m endpoint_ledger.cli run-suite --suite smoke --provider mock --dry-run

# A run that simulates intermittent failures (shows "completed_with_errors").
python -m endpoint_ledger.cli run-suite --suite smoke --provider mock --repeats 3
```

To change mock behavior, edit the seeded endpoint's `request_defaults.mock.mode`
in the database, or add a `local_endpoints/*.yaml` file:

```yaml
# local_endpoints/my-mock.yaml  (gitignored, loaded by `seed`)
provider: mock
display_name: "My Regression Mock"
model_requested: "mock:model-1"
request_defaults:
  mock:
    mode: structured_output_regression
    seed: 1
```

## Optional live OpenAI run

A live OpenAI run is only possible when you **explicitly** request it:

```bash
# 1. Put a real key in the environment (or .env):
#    OPENAI_API_KEY=sk-...

# 2. Run with the --live flag:
python -m endpoint_ledger.cli run-suite --suite smoke --provider openai --live
```

Without `OPENAI_API_KEY` the OpenAI endpoint reports a clear **unavailable**
state instead of pretending to run. Without `--live`, the runner refuses to call
a live provider.

You can also gate by estimated cost:

```bash
python -m endpoint_ledger.cli run-suite --suite smoke --provider openai --live \
  --cost-ceiling 0.50     # refuses to start if the estimated max cost exceeds $0.50
```

## Change detection

```bash
# Generate clean demo history: 30 days of stable behavior, then a persistent
# structured-output regression, then detect and persist provisional events.
python -m endpoint_ledger.cli generate-demo-history

# Or run detection yourself, with an explicit baseline/current boundary.
python -m endpoint_ledger.cli detect-changes --split-after 2026-08-01T00:00:00
```

> Note: `generate-demo-history` leaves the mock endpoint in the regression mode
> it simulated, so the change event stays visible. Re-run `make seed` to reset
> the mock endpoint to `stable_success` before your next green `make mock-run`.

Change detection is deliberately conservative:

- **Live behavioral claims** are made only for *primary* outcome metrics of
  baseline-eligible probe versions, only when the matching baseline cohort is
  qualified, all comparison dimensions match (probe version, scorer version,
  provider-reported model), and no measurement defect is unresolved. Otherwise
  the detector **refuses** and records why.
- Demo/mock detection runs through an explicit non-live origin
  (`--origin mock|demo|test`) and is clearly labelled — it never contaminates
  live claims.
- Binary metrics use Fisher's exact test; latency is compared by medians
  (Mann–Whitney U when samples allow) and is kept separate from quality
  metrics. No *confirmed* latency event in Phase 1.

## Measurement integrity and baselines

```bash
# Audit every failed scored check of a run (persists adjudications, writes a report).
python -m endpoint_ledger.cli audit-run 6

# Show baseline cohort qualification progress.
python -m endpoint_ledger.cli baseline-status --provider openai

# Readiness check; never calls the provider.
python -m endpoint_ledger.cli baseline-preflight --provider openai --endpoint gpt-4o-mini \
  --suite smoke --repeats 5

# Collect baseline data (eligible probe versions only, prints fingerprints).
python -m endpoint_ledger.cli run-baseline --provider openai --endpoint gpt-4o-mini \
  --suite smoke --repeats 5 --cost-ceiling 1.00 --live

# First post-change verification run (1 repeat/probe, requires manual approval).
python -m endpoint_ledger.cli run-baseline --provider openai --endpoint gpt-4o-mini \
  --suite smoke --cost-ceiling 1.00 --live --qualification-smoke
python -m endpoint_ledger.cli approve-qualification-run <run-id>
```

See `docs/baseline-collection.md` for the recommended collection cadence
(five repeats per probe, twice per day, for seven days — a proposed MVP policy).

## Evidence durability

```bash
# Database backups (see docs/backup-and-recovery.md).
make db-backup
make db-list-backups
make db-verify-backup BACKUP=.backups/<file>.dump
make db-restore BACKUP=.backups/<file>.dump

# Portable run evidence bundles.
python -m endpoint_ledger.cli export-run <run-id>
python -m endpoint_ledger.cli verify-run-export <path>
python -m endpoint_ledger.cli import-run-export <path> --target-empty-db
```

Live evidence is append-only once sealed: after a live run reaches a terminal
state it is sealed, and the database blocks updates and deletions of the run,
its observations, and its scores — even via direct SQL. `run-baseline` then
exports, verifies, and archives a portable bundle; if the export fails, the
sealed database evidence is preserved and the run is marked `export_failed`.
See `docs/incidents/2026-08-09-demo-history-data-loss.md` for the incident that
motivated these protections.

## Dashboard

```bash
python -m endpoint_ledger.cli serve          # http://127.0.0.1:8000
# or
make serve
```

Pages: Overview, Endpoint detail, Suite run detail, Probe detail, Observation
detail, Change events. Every number is computed from the database at request
time — no hardcoded metrics or placeholder charts.

Raw provider responses are **hidden** in the web UI by default. To view them
locally set `ENDPOINT_LEDGER_SHOW_RAW_RESPONSES=true`. Keep it false outside
local development.

## Test, lint, type-check

```bash
make test          # pytest (unit + DB integration + dashboard routes)
make lint          # ruff
make typecheck     # mypy
make check         # ruff + mypy + pytest (everything non-live)
```

Live API tests are opt-in and excluded from the default suite.

## How to add a probe

1. Add a YAML file to `probes/smoke/` (or point a new suite directory at it).
2. Give it a unique `stable_key`, a `version`, a `category`, and `messages`.
3. Declare `expected_behavior` in machine-verifiable terms (JSON Schema,
   required fields, expected tool calls, forbidden tools, item count, or the
   constrained confirmation contract).
4. List `scorers` (names from the deterministic registry; unknown names fail
   loading).
5. Run `python -m endpoint_ledger.cli seed`. A changed definition creates a new
   **immutable** `ProbeVersion`; existing observations keep referencing the old
   version.

## How to add a provider adapter

1. Subclass `ProviderAdapter` in `endpoint_ledger/providers/`.
2. Implement `is_available()` and `invoke(endpoint_config, probe_version,
   repeat_index) -> CanonicalProviderResponse`.
3. Preserve the provider's raw response and capture request ID / model / usage.
4. Never log or store the API key; sanitize error messages before persistence.
5. Register the adapter in `endpoint_ledger/providers/__init__.py`.

The runner and database model do not change when a provider is added.

## Security and data handling

- Secrets are loaded **only from the environment** (`OPENAI_API_KEY` etc.) or an
  explicitly configured secret store. The application never searches personal
  files, notes, documents, shell history, or unrelated directories for
  credentials. `run-baseline` refuses to run when the key is not in the
  environment.
- **Use a dedicated secret manager and a freshly rotated key.** The OpenAI key
  is a live credential: store it in your shell profile, a secrets manager
  (e.g. 1Password CLI, `op run`, or a CI secret store), or the gitignored
  `.env` file — and rotate it immediately if it has ever been shared in plain
  text. No key value ever appears in CLI arguments, command output, exception
  persistence, database rows, raw request objects, or test snapshots.
- Authorization headers and API-key-shaped values are redacted defensively
  (`sanitize.py`) before any persistence.
- Provider exception messages are sanitized before storage.
- Synthetic data only — no real PII in probes or fixtures.
- The dashboard is a local/private server-rendered app; raw responses default to
  hidden.

## Known limitations (Phase 1)

- Only two providers: `mock` and `openai`. Anthropic and Google are designed for
  but not yet implemented.
- The single-invocation harness does **not** execute multi-turn tool workflows.
  `tool_calling_lookup_then_cancel` v1 is marked baseline-ineligible for that
  reason (its results measure single-turn behavior only); v2 tests a genuinely
  single-turn tool-selection behavior. Multi-turn agent testing is a later
  phase.
- The OpenAI adapter is unit-tested against recorded synthetic fixtures. If
  credentials are absent it reports unavailable. Live runs are always opt-in
  (`--live`) and cost-gated.
- Sequential execution only. Correctness over concurrency.
- Live change claims use **run-level** statistics: the five repeats inside one
  SuiteRun are technical replicates, not independent time samples. A single
  SuiteRun can never confirm a change, regardless of how many repeats it
  contains.
- Probe versioning is append-only and protected by a DB trigger; version rows
  cannot be updated or deleted once observations exist. Sealed live evidence is
  protected by additional DB triggers.
- Cost accounting requires endpoint pricing to be configured; otherwise cost is
  reported as "unknown" rather than invented. Historical cost is never
  recomputed with future pricing — each observation carries its pricing
  snapshot.
- Runs 5 and 6 (the first live OpenAI evidence) were lost in the 2026-08-09
  demo-history incident; see `docs/incidents/2026-08-09-demo-history-data-loss.md`.
  They are not recreated or fabricated.
