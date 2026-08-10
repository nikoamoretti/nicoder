# Endpoint Ledger — Architecture

## Components

```
┌────────────────────────────────────────────────────────────────────────────┐
│ CLI (Typer)                                                               │
│   db upgrade · seed · run-suite · detect-changes · generate-demo-history  │
│   serve                                                                   │
└──────┬───────────────────────────────┬────────────────────────────────────┘
       │                               │
       ▼                               ▼
┌───────────────────┐       ┌─────────────────────┐
│ SuiteRunner       │       │ Web dashboard       │
│  matrix           │       │ (FastAPI + Jinja)   │
│  cost ceiling     │       │  reads DB only      │
│  dry-run          │       └──────────┬──────────┘
└───────┬───────────┘                  │
        │ invoke( )                    │
        ▼                              ▼
┌───────────────────┐       ┌─────────────────────┐
│ ProviderAdapter   │       │ PostgreSQL          │
│  MockProvider     │       │  endpoints          │
│  OpenAIAdapter    │       │  probes/versions    │
└───────────────────┘       │  suite_runs         │
        │                   │  observations       │
        ▼                   │  scores             │
┌───────────────────┐       │  change_events      │
│ CanonicalResponse │──────▶│  (immutability trig)│
│  scoring (pure)   │       └─────────────────────┘
└───────────────────┘
```

## Data flow

1. Probe YAML files are loaded and validated (`probes/loader.py`). Each unique
   definition hash becomes an immutable `ProbeVersion`; re-running `seed` is a
   no-op.
2. `SuiteRunner` builds the endpoint × probe × repeat matrix, computes a maximum
   cost estimate (unknown when pricing is not configured), and creates a
   `SuiteRun`.
3. For each matrix item the provider adapter is invoked and returns a
   `CanonicalProviderResponse`. Provider-specific raw response, request id,
   model, and usage are preserved.
4. Every observation is persisted — including failures — with hashed raw request
   and raw response. Deterministic scorers produce one `Score` row per metric.
5. `change_detection/service.py` compares baseline vs current windows and, only
   when minimum samples + change threshold + p-value are all met, upserts a
   provisional `ChangeEvent`.
6. The dashboard reads only from the database. Raw responses are hidden unless
   `ENDPOINT_LEDGER_SHOW_RAW_RESPONSES=true`.

## Provider abstraction

`ProviderAdapter` (in `providers/base.py`) is the only seam between providers
and the core:

```python
class ProviderAdapter(ABC):
    provider_name: str
    def is_available(self) -> bool: ...
    def invoke(self, endpoint_config, probe_version, repeat_index) -> CanonicalProviderResponse: ...
```

Adding a provider = implement this + register it in `providers/__init__.py`.
The runner and DB model do not change. Provider-specific behavior (raw response
shape, request IDs, token accounting, error taxonomy) is preserved as data — we
deliberately do **not** use a normalization gateway like LiteLLM.

The **MockProvider** implements deterministic scenario modes
(`stable_success`, `stable_failure`, `structured_output_regression`,
`tool_selection_regression`, `high_latency`, `intermittent_errors`) so tests and
the change-detection demo are reproducible. Its behavior is a pure function of
(seed, endpoint id, probe version id, repeat index).

The **OpenAIAdapter** reads `OPENAI_API_KEY` from the environment, uses the
official SDK, retries only transient failures with bounded exponential backoff
(never validation failures), and reports unavailable when the key is absent.

## Probe versioning

- `Probe.stable_key` identifies the probe; `ProbeVersion` holds one immutable
  snapshot per definition hash.
- A modified probe file creates a new `ProbeVersion`; the old one keeps its
  observations.
- A Postgres trigger blocks UPDATE/DELETE on any `ProbeVersion` that has
  observations. Tests recreate the same trigger via
  `endpoint_ledger/db/triggers.py`, which the Alembic migration also uses.

## Scoring

- Every scorer is a pure function of `(CanonicalProviderResponse,
  expected_behavior)` with stable `name`, `version`, `metric_name`, and a
  structured `details` payload.
- Probes declare exactly which scorers run (`scorers` in YAML); unknown names
  fail probe loading.
- Confirmation detection uses an explicit constrained contract (a small JSON
  response with `action_status` enum + `confirmation_required` boolean) — never
  an open-ended semantic classifier.

## Change detection

Conservative by design:

- Binary metrics: baseline vs current success rate, Fisher's exact test,
  absolute-change threshold, and minimum sample counts. All must pass before a
  *provisional* event is created.
- Latency: median comparison (Mann–Whitney U when samples allow), tracked as a
  distinct metric. No *confirmed* latency event in Phase 1.
- Events are upserted per (endpoint, metric, window bounds); re-running
  detection refreshes rather than duplicates.
- Nothing is ever called confirmed from one changed response.

## Security

- Secrets live in environment variables / `.env` (gitignored). API keys are
  never logged, persisted, or rendered.
- `sanitize.py` redacts `sk-...` patterns, known secret values, and auth-style
  fields from exception messages and stored payloads.
- `build_request_payload` redacts secret-shaped fields defensively before the
  request is persisted.
- Raw responses are hidden in the dashboard by default; the switch defaults to
  false outside local development.

## Future scheduler and object-storage migration

- **Scheduler:** a lightweight process (cron or a long-running loop inside the
  existing runner) will trigger suites on a cadence and call the same
  `detect_changes` service; no new orchestration stack is planned.
- **Object storage:** raw responses will move out of PostgreSQL rows into
  object storage (e.g. Tigris/S3) with the database keeping hashes and links.
  The `raw_response` column already separates provider-shaped data from
  normalized fields, so the web UI continues to work unchanged.

## Evidence durability (Phase 1.2)

```
live SuiteRun (open)
      │  observations + scores persisted
      ▼
terminal status → SEALED (evidence_state=sealed, sealed_at)
      │  DB triggers block UPDATE/DELETE of run/observations/scores
      │  (direct SQL included); derived records may still be added
      ▼
export-run → portable bundle (manifest + per-file SHA-256) → verify
      │  RunArtifact row; secret scan before valid
      ▼
exported (evidence_manifest_hash, evidence_exported_at)
   └─ export failure → export_failed (sealed evidence preserved)
```

Destructive maintenance goes through a shared guard (dry-run default,
`--execute`, origin verification, transaction scope, audit log,
recent-verified-backup requirement when live evidence exists). Backups use
`pg_dump`/`pg_restore` with verification in a temporary database
(`endpoint_ledger/backup.py`); run bundles are portable JSON/JSONL evidence
artifacts (`endpoint_ledger/export.py`).

Live change claims use run-level statistics: repeats within one SuiteRun are
technical replicates, so live binary comparisons aggregate per SuiteRun and
require minimum run counts, minimum current UTC dates, a material effect
threshold, direction consistency across current runs, and a permutation test
with Benjamini-Hochberg correction across the metric batch.

## Future scheduler and object-storage migration

- **Scheduler:** a lightweight process (cron or a long-running loop inside the
  existing runner) will trigger suites on a cadence and call the same
  `detect_changes` service; no new orchestration stack is planned.
- **Object storage:** raw responses will move out of PostgreSQL rows into
  object storage (e.g. Tigris/S3) with the database keeping hashes and links.
  The `raw_response` column already separates provider-shaped data from
  normalized fields, so the web UI continues to work unchanged.
- **Multi-turn workflows:** the single-invocation harness cannot execute
  multi-turn tool sequences; a proper tool-execution loop is a later phase.
