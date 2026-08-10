# Endpoint Ledger — Product Spec

## Problem

Teams ship production applications that depend on externally served AI models.
These systems change without notice: a model is swapped, a prompt template is
tuned, a deprecation lands, or an upstream host degrades. The change is rarely
announced and almost never measured against *your* use of the endpoint. Today,
when a production integration "mysteriously breaks," nobody can say when the
provider changed, what exactly changed, or whether it was the model or the
request. Reproducibility is impossible because no one recorded the exact
endpoint, request parameters, timestamps, and observable behaviors.

## User

A founding engineer (or small platform/ML team) who operates LLM-powered product
features and needs a trustworthy answer to the question: **"Is the model I
depend on still behaving the way it did last week?"** Phase 1 targets a single
operator at a laptop, not an organization-wide SRE platform.

## MVP question

> Can we repeatedly test a live AI model endpoint, deterministically score
> production-relevant behavior, keep an auditable historical record, detect
> material changes, and inspect the evidence — all with a small, boring,
> local-first tool?

## Core data object

> A specific AI endpoint, through a specific provider, at a specific timestamp,
> under exact request parameters, returned an observable behavior that can be
> scored and compared with prior observations.

Every record is source-linked: endpoint id, probe version (immutable, hashed),
repeat index, raw request hash, raw response hash, and timestamps. Nothing is
aggregated away before it is stored.

## Metrics (Phase 1)

1. Structured-output reliability — response parses as JSON, validates against a
   declared JSON Schema, contains required fields, and has no unexpected fields.
2. Tool-calling reliability — correct tool selected, forbidden tools avoided,
   arguments valid and exact (or schema-valid), required call sequence honored.
3. Instruction adherence — exact item counts, no explanatory prose, and a
   constrained confirmation contract for destructive actions.
4. Operational behavior — HTTP success, error occurrence, latency, token usage,
   and cost (when pricing is configured).

Every scorer is deterministic and independently unit-tested. No LLM judges.

## Non-goals (Phase 1)

- Anthropic, Google Gemini, Azure OpenAI, Bedrock, Vertex AI
- User authentication (local/private dashboard only)
- Billing, Slack/email notifications, public API access
- LLM-as-a-judge scoring, general intelligence benchmarks
- Customer-created probes, public leaderboards
- Multi-region execution, distributed workers, production deployment
- Automatic crawling of provider release notes

## Future phases

- **Phase 2 — confirmations and cadence:** scheduler that runs suites on a
  fixed cadence, auto-promotion of provisional events to confirmed, and
  per-endpoint alerting (Slack/email).
- **Phase 3 — provider breadth:** Anthropic and Google adapters, streaming
  probes, vision/multimodal probes, and structured-output-mode specific probes.
- **Phase 4 — object storage & long-range history:** raw responses migrated to
  object storage (raw responses hidden by default), longer retention, and
  historical backfill of provider releases for correlation (never causation).
- **Phase 5 — organization:** auth, shared dashboards, customer-created probes
  with approval, and multi-tenant isolation.
