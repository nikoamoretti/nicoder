"""Suite runner.

Builds the endpoint x probe x repeat matrix, persists every observation even
when it fails, continues after individual failures, and marks the final suite
status accurately. Dry-run prints the matrix without calling any API or writing
to the database.
"""

from __future__ import annotations

import hashlib
import json
import time
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any

from sqlalchemy import or_, select
from sqlalchemy.orm import Session

from endpoint_ledger import __version__
from endpoint_ledger.config import Settings, get_settings
from endpoint_ledger.db.models import (
    Observation,
    Probe,
    ProbeVersion,
    ProviderEndpoint,
    Score,
    SuiteRun,
)
from endpoint_ledger.measurement.origins import DataOrigin, derive_origin
from endpoint_ledger.probes.loader import compute_definition_hash, load_probes, sync_probes_to_db
from endpoint_ledger.providers import get_provider
from endpoint_ledger.providers.base import ProviderAdapter, ProviderUnavailableError
from endpoint_ledger.runner.canonical_response import CanonicalProviderResponse
from endpoint_ledger.runner.request_builder import build_request_payload
from endpoint_ledger.sanitize import redact_secrets, sanitize_message
from endpoint_ledger.scoring import score_response
from endpoint_ledger.services.credentials import CredentialResolutionError, resolve_credential


class CostCeilingExceededError(RuntimeError):
    pass


class CredentialBindingError(RuntimeError):
    """Live execution refused: credential does not match the last preflight."""


@dataclass
class RunItem:
    endpoint: ProviderEndpoint
    probe_version: ProbeVersion
    repeat_index: int


@dataclass
class SuiteOptions:
    suite_name: str = "smoke"
    provider: str | None = None
    endpoint_filter: str | None = None
    repeats: int = 3
    delay_seconds: float = 0.0
    dry_run: bool = False
    live: bool = False
    cost_ceiling_usd: float | None = None
    probes_dir: str | None = None
    # Measurement integrity (Phase 1.1)
    data_origin: DataOrigin | None = None
    eligible_only: bool = False


@dataclass
class RunSummary:
    suite_run: SuiteRun | None
    planned_observations: int
    estimate_usd: float | None
    items: list[RunItem] = field(default_factory=list)


def _utcnow() -> datetime:
    return datetime.now(UTC)


def _json_hash(payload: Any) -> str:
    canonical = json.dumps(payload, sort_keys=True, separators=(",", ":"), default=str)
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def _estimate_text_tokens(messages: list[dict[str, Any]]) -> int:
    total_chars = 0
    for message in messages:
        total_chars += len(message.get("content") or "")
    return max(1, total_chars // 4)


def _latest_eligible_versions(versions: list[ProbeVersion]) -> list[ProbeVersion]:
    """Only the newest baseline-eligible version of each probe."""
    latest: dict[str, ProbeVersion] = {}
    for version in versions:
        if not version.baseline_eligible:
            continue
        key = version.probe.stable_key
        existing = latest.get(key)
        if existing is None or version.version_number > existing.version_number:
            latest[key] = version
    return sorted(latest.values(), key=lambda v: v.probe.stable_key)


class SuiteRunner:
    def __init__(self, session: Session, settings: Settings | None = None) -> None:
        self.session = session
        self.settings = settings or get_settings()

    # ------------------------------------------------------------------ plan

    def _resolve_versions(self, options: SuiteOptions) -> list[ProbeVersion]:
        probes_dir = options.probes_dir or str(self.settings.probes_path)
        definitions = load_probes(probes_dir)
        if options.dry_run:
            versions: list[ProbeVersion] = []
            for d in definitions:
                probe = Probe(
                    stable_key=d.stable_key,
                    category=d.category,
                    title=d.title,
                    description=d.description,
                )
                version = ProbeVersion(
                    probe_id=0,
                    version_number=d.version,
                    definition_hash=compute_definition_hash(d),
                    prompt_messages=[m.model_dump() for m in d.messages],
                    tools=d.tools,
                    expected_behavior=d.expected_behavior,
                    request_parameters=d.request_parameters,
                    scorers=d.scorers,
                    primary_metric=None,
                )
                version.probe = probe
                versions.append(version)
            if options.eligible_only:
                versions = [v for v in versions if v.baseline_eligible]
            return versions
        versions = sync_probes_to_db(self.session, definitions)
        if options.eligible_only:
            versions = _latest_eligible_versions(versions)
        return versions

    def _query_endpoints(self, options: SuiteOptions) -> list[ProviderEndpoint]:
        query = select(ProviderEndpoint).where(ProviderEndpoint.enabled.is_(True))
        if options.provider:
            query = query.where(ProviderEndpoint.provider == options.provider)
        if options.endpoint_filter:
            query = query.where(
                or_(
                    ProviderEndpoint.display_name == options.endpoint_filter,
                    ProviderEndpoint.model_requested == options.endpoint_filter,
                )
            )
        return list(self.session.scalars(query.order_by(ProviderEndpoint.id)))

    def plan(self, options: SuiteOptions) -> RunSummary:
        versions = self._resolve_versions(options)
        endpoints = self._query_endpoints(options)
        items: list[RunItem] = []
        for endpoint in endpoints:
            for version in versions:
                for repeat in range(1, max(1, options.repeats) + 1):
                    items.append(RunItem(endpoint=endpoint, probe_version=version, repeat_index=repeat))
        estimate = self.estimate_max_cost(items)
        return RunSummary(suite_run=None, planned_observations=len(items), estimate_usd=estimate, items=items)

    # ------------------------------------------------------------------ cost

    @staticmethod
    def _estimate_item_cost(endpoint: ProviderEndpoint, version: ProbeVersion) -> float | None:
        if (
            endpoint.input_cost_per_million_tokens is None
            or endpoint.output_cost_per_million_tokens is None
        ):
            return None
        input_tokens = _estimate_text_tokens(version.prompt_messages)
        output_tokens = int(version.request_parameters.get("max_tokens") or 2048)
        cost = (
            input_tokens / 1_000_000 * endpoint.input_cost_per_million_tokens
            + output_tokens / 1_000_000 * endpoint.output_cost_per_million_tokens
        )
        return round(cost, 6)

    def estimate_max_cost(self, items: list[RunItem]) -> float | None:
        total = 0.0
        for item in items:
            item_cost = self._estimate_item_cost(item.endpoint, item.probe_version)
            if item_cost is None:
                return None
            total += item_cost
        return round(total, 6)

    @staticmethod
    def _observation_cost(endpoint: ProviderEndpoint, response: CanonicalProviderResponse) -> float | None:
        if (
            endpoint.input_cost_per_million_tokens is None
            or endpoint.output_cost_per_million_tokens is None
        ):
            return None
        if response.usage.input_tokens is None or response.usage.output_tokens is None:
            return None
        cost = (
            response.usage.input_tokens / 1_000_000 * endpoint.input_cost_per_million_tokens
            + response.usage.output_tokens / 1_000_000 * endpoint.output_cost_per_million_tokens
        )
        return round(cost, 8)

    # ------------------------------------------------------------------ run

    def run(self, options: SuiteOptions) -> RunSummary:
        versions = self._resolve_versions(options)
        endpoints = self._query_endpoints(options)
        items: list[RunItem] = []
        for endpoint in endpoints:
            for version in versions:
                for repeat in range(1, max(1, options.repeats) + 1):
                    items.append(RunItem(endpoint=endpoint, probe_version=version, repeat_index=repeat))

        if not items:
            raise ValueError("no enabled endpoints/probes matched the run options")

        estimate = self.estimate_max_cost(items)
        summary = RunSummary(suite_run=None, planned_observations=len(items), estimate_usd=estimate, items=items)

        if options.dry_run:
            print("DRY RUN — planned request matrix (no APIs called, nothing persisted)")
            for item in items:
                print(
                    f"  {item.endpoint.provider:8s} {item.endpoint.display_name:30s} "
                    f"{item.probe_version.probe.stable_key:55s} repeat {item.repeat_index}"
                )
            if estimate is None:
                print("  estimated cost: unknown (endpoint pricing not configured)")
            else:
                print(f"  estimated max cost: ${estimate:.6f}")
            return summary

        for provider_name in sorted({e.provider for e in endpoints}):
            if provider_name == "openai" and not options.live:
                return self._fail_before_start(
                    options, len(items), estimate,
                    "openai is a live provider; re-run with --live to allow real API calls",
                )
            try:
                self._guard_live_credential_binding(provider_name, endpoints)
            except (CredentialResolutionError, CredentialBindingError) as exc:
                return self._fail_before_start(
                    options, len(items), estimate, sanitize_message(str(exc))
                )
            adapter = get_provider(provider_name, self.settings, session=self.session)
            if not adapter.is_available():
                return self._fail_before_start(
                    options, len(items), estimate,
                    f"provider {provider_name!r} is unavailable (check credentials); run not started",
                )

        ceiling = options.cost_ceiling_usd
        if estimate is not None and ceiling is not None and estimate > ceiling:
            raise CostCeilingExceededError(
                f"estimated maximum cost ${estimate:.6f} exceeds ceiling ${ceiling:.6f}; run refused"
            )

        run = SuiteRun(
            suite_name=options.suite_name,
            status="running",
            started_at=_utcnow(),
            provider_filter=options.provider,
            endpoint_filter=options.endpoint_filter,
            total_expected_observations=len(items),
            estimated_cost_usd=estimate,
            runner_version=__version__,
            data_origin=derive_origin(
                options.provider, options.suite_name, explicit=options.data_origin
            ),
        )
        self.session.add(run)
        self.session.flush()

        adapters: dict[str, ProviderAdapter] = {
            name: get_provider(name, self.settings, session=self.session)
            for name in {e.provider for e in endpoints}
        }

        completed = 0
        failed = 0
        try:
            for index, item in enumerate(items):
                adapter = adapters[item.endpoint.provider]
                observation: Observation | None = None
                try:
                    response = adapter.invoke(item.endpoint, item.probe_version, item.repeat_index)
                    observation = self._persist_observation(run, item, response)
                except ProviderUnavailableError as exc:
                    observation = self._persist_failed_observation(
                        run, item, error_type="provider_unavailable", error_message=sanitize_message(str(exc))
                    )
                except Exception as exc:
                    observation = self._persist_failed_observation(
                        run, item, error_type=type(exc).__name__, error_message=sanitize_message(str(exc))
                    )
                if observation is not None and observation.error_type is not None:
                    failed += 1
                completed += 1
                if options.delay_seconds and index < len(items) - 1:
                    time.sleep(options.delay_seconds)

            run.completed_observations = completed
            run.failed_observations = failed
            actual_costs = [
                obs.estimated_cost_usd
                for obs in run.observations
                if obs.estimated_cost_usd is not None
            ]
            if len(actual_costs) == len(items):
                run.actual_cost_usd = round(sum(actual_costs), 6)
            if completed == len(items):
                run.status = "completed" if failed == 0 else "completed_with_errors"
            else:
                run.status = "failed"
                run.error_summary = "observations are missing; run did not complete all attempts"
            run.completed_at = _utcnow()
            from endpoint_ledger.evidence import seal_run

            seal_run(self.session, run)
            self.session.commit()
        except Exception as exc:  # pragma: no cover - defensive
            self.session.rollback()
            run.status = "failed"
            run.completed_observations = completed
            run.failed_observations = failed
            run.error_summary = sanitize_message(str(exc))
            run.completed_at = _utcnow()
            self.session.add(run)
            from endpoint_ledger.evidence import seal_run

            seal_run(self.session, run)
            self.session.commit()

        summary.suite_run = run
        return summary

    def _guard_live_credential_binding(
        self,
        provider_name: str,
        endpoints: list[ProviderEndpoint],
    ) -> None:
        """Block a live run whose credential does not match the last preflight.

        Applies to live providers only. A mismatch raises
        ``CredentialBindingError("CREDENTIAL CHANGED SINCE PREFLIGHT")``
        before any provider request is made.
        """
        if provider_name != "openai":
            return
        from endpoint_ledger.services.preflight_service import latest_preflight

        credential = resolve_credential(self.session, provider_name, self.settings)
        record = latest_preflight(self.session, provider_name)
        if record is None:
            raise CredentialBindingError("PREFLIGHT REQUIRED BEFORE LIVE RUN")
        if not record.ready:
            raise CredentialBindingError("preflight is not ready")
        if record.binding_valid is False:
            raise CredentialBindingError("CREDENTIAL CHANGED SINCE PREFLIGHT")
        if record.credential_fingerprint != credential.fingerprint:
            raise CredentialBindingError("CREDENTIAL CHANGED SINCE PREFLIGHT")

    def _fail_before_start(
        self,
        options: SuiteOptions,
        total: int,
        estimate: float | None,
        message: str,
    ) -> RunSummary:
        run = SuiteRun(
            suite_name=options.suite_name,
            status="failed",
            started_at=_utcnow(),
            completed_at=_utcnow(),
            provider_filter=options.provider,
            endpoint_filter=options.endpoint_filter,
            total_expected_observations=total,
            completed_observations=0,
            failed_observations=0,
            estimated_cost_usd=estimate,
            runner_version=__version__,
            error_summary=message,
            data_origin=derive_origin(
                options.provider, options.suite_name, explicit=options.data_origin
            ),
        )
        self.session.add(run)
        from endpoint_ledger.evidence import seal_run

        seal_run(self.session, run)
        self.session.commit()
        summary = RunSummary(suite_run=run, planned_observations=total, estimate_usd=estimate)
        return summary

    # ------------------------------------------------------------- persistence

    def _persist_observation(
        self,
        run: SuiteRun,
        item: RunItem,
        response: CanonicalProviderResponse,
    ) -> Observation:
        raw_request = build_request_payload(item.endpoint, item.probe_version)
        raw_response = redact_secrets(response.raw_response)
        pricing_effective = _utcnow()
        observation = Observation(
            suite_run_id=run.id,
            endpoint_id=item.endpoint.id,
            probe_version_id=item.probe_version.id,
            repeat_index=item.repeat_index,
            requested_at=response.request_started_at or _utcnow(),
            completed_at=response.request_completed_at,
            latency_ms=response.latency_ms,
            http_status=response.http_status,
            model_reported=response.model_reported,
            finish_reason=response.finish_reason,
            raw_request=raw_request,
            raw_response=raw_response,
            raw_request_hash=_json_hash(raw_request),
            raw_response_hash=_json_hash(raw_response) if raw_response is not None else None,
            normalized_text=response.text,
            normalized_tool_calls=response.tool_calls_dict or None,
            input_tokens=response.usage.input_tokens,
            output_tokens=response.usage.output_tokens,
            estimated_cost_usd=self._observation_cost(item.endpoint, response),
            error_type=response.error.error_type if response.error else None,
            error_message_sanitized=response.error.message_sanitized if response.error else None,
            provider_request_id=response.provider_request_id,
            # Measurement integrity (Phase 1.1)
            error_code=response.error.error_code if response.error else None,
            error_category=response.error.error_category if response.error else None,
            pricing_input_price_per_million=item.endpoint.input_cost_per_million_tokens,
            pricing_output_price_per_million=item.endpoint.output_cost_per_million_tokens,
            pricing_effective_at=pricing_effective,
            cost_calculation_version="1.0.0",
            cost_is_estimated=False,
        )
        self.session.add(observation)
        self.session.flush()

        scorer_names = item.probe_version.scorers
        expected = item.probe_version.expected_behavior
        results = score_response(
            response, scorer_names, expected, primary_metric=item.probe_version.primary_metric
        )
        for result in results:
            self.session.add(
                Score(
                    observation_id=observation.id,
                    scorer_name=result.scorer_name,
                    scorer_version=result.scorer_version,
                    metric_name=result.metric_name,
                    passed=result.passed,
                    numeric_value=result.numeric_value,
                    explanation=result.explanation,
                    details=result.details,
                    role=result.role,
                )
            )
        self.session.flush()
        return observation

    def _persist_failed_observation(
        self,
        run: SuiteRun,
        item: RunItem,
        error_type: str,
        error_message: str,
    ) -> Observation:
        from endpoint_ledger.measurement.taxonomy import classify_provider_error

        raw_request = build_request_payload(item.endpoint, item.probe_version)
        category, code = classify_provider_error(error_type, error_message, None)
        observation = Observation(
            suite_run_id=run.id,
            endpoint_id=item.endpoint.id,
            probe_version_id=item.probe_version.id,
            repeat_index=item.repeat_index,
            requested_at=_utcnow(),
            raw_request=raw_request,
            raw_request_hash=_json_hash(raw_request),
            error_type=error_type,
            error_message_sanitized=error_message,
            error_code=code,
            error_category=category,
        )
        self.session.add(observation)
        self.session.flush()
        return observation
