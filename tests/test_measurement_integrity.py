"""Phase 1.1 tests: roles, origins, taxonomy, cohorts, adjudication, stats."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest
from sqlalchemy import create_engine, text

from endpoint_ledger.change_detection.service import (
    detect_changes,
)
from endpoint_ledger.config import PROJECT_ROOT, Settings
from endpoint_ledger.db.base import Base
from endpoint_ledger.db.models import (
    BaselineCohort,
    ModelResolutionEvent,
    Observation,
    ProbeVersion,
    Score,
    ScoreAdjudication,
    SuiteRun,
)
from endpoint_ledger.measurement.adjudication import (
    FAILURE_CLASSIFICATIONS,
    default_failure_classification,
    persist_adjudications,
)
from endpoint_ledger.measurement.cohorts import (
    CohortService,
    cohort_fingerprint,
    observation_baseline_eligible,
    primary_outcome_for_observation,
)
from endpoint_ledger.measurement.origins import derive_origin
from endpoint_ledger.measurement.roles import (
    INFORMATIONAL_ROLE,
    PRIMARY_ROLE,
    primary_metric_for_definition,
    role_for_metric,
)
from endpoint_ledger.measurement.stats import wilson_interval
from endpoint_ledger.measurement.taxonomy import classify_provider_error
from endpoint_ledger.runner.service import SuiteOptions, SuiteRunner
from tests.conftest import TEST_DATABASE_URL
from tests.helpers import load_smoke_probes, make_endpoint, sync_probes

SMALL_POLICY = dict(
    baseline_min_calendar_days=0,
    baseline_min_distinct_runs=2,
    baseline_min_primary_observations=8,
    baseline_min_distinct_dates=1,
    baseline_min_elapsed_hours=0.0,
)


def _small_settings() -> Settings:
    return Settings(database_url=TEST_DATABASE_URL, **SMALL_POLICY)


def _run_mock_phase(
    db, settings, endpoint, mode, suite_name, repeats=5, backdate_days=None,
    success_rate=1.0, endpoint_filter=None,
):
    endpoint.request_defaults = {"mock": {"mode": mode, "seed": 5, "success_rate": success_rate}}
    db.commit()
    run = SuiteRunner(db, settings).run(
        SuiteOptions(
            suite_name=suite_name, provider="mock", repeats=repeats, live=False,
            endpoint_filter=endpoint_filter or endpoint.display_name,
        )
    ).suite_run
    if backdate_days:
        offset = timedelta(days=backdate_days)
        run.started_at = run.started_at - offset
        if run.completed_at:
            run.completed_at = run.completed_at - offset
        for observation in run.observations:
            observation.requested_at = observation.requested_at - offset
            if observation.completed_at is not None:
                observation.completed_at = observation.completed_at - offset
        db.commit()
    return run


# ---------------------------------------------------------------- taxonomy


def test_insufficient_quota_is_account_configuration():
    category, code = classify_provider_error(
        "RateLimitError", "Error code: 429 - insufficient_quota, credit_balance_exhausted", 429
    )
    assert category == "account_configuration"
    assert code == "insufficient_quota"


def test_taxonomy_codes():
    cases = [
        (("APITimeoutError", "timeout", None), ("provider_transport", "provider_timeout")),
        (("APIConnectionError", "conn refused", None), ("provider_transport", "provider_connection_error")),
        (("InternalServerError", "500", 500), ("provider_transport", "provider_5xx")),
        (("RateLimitError", "rate limit exceeded", 429), ("provider_transport", "provider_rate_limit")),
        (("AuthenticationError", "Invalid API key provided", 401), ("account_configuration", "invalid_api_key")),
        (("BadRequestError", "bad request", 400), ("account_configuration", "invalid_account_configuration")),
        (("OddError", "something", None), ("unknown", "unclassified_error")),
    ]
    for (error_type, message, status), expected in cases:
        assert classify_provider_error(error_type, message, status) == expected


# ---------------------------------------------------------------- roles


def test_exactly_one_primary_metric_per_probe_definition():
    for definition in load_smoke_probes():
        primary = primary_metric_for_definition(definition.category, definition.expected_behavior)
        assert primary in definition.scorers, f"{definition.stable_key} missing primary scorer"
        # exactly one: the primary metric is a single string; no other scorer
        # claims the primary role.
        for name in definition.scorers:
            if name == primary:
                continue
            assert role_for_metric(name, primary) != PRIMARY_ROLE


def test_role_assignment():
    assert role_for_metric("structured_output_task_success", "structured_output_task_success") == PRIMARY_ROLE
    assert role_for_metric("json_parse_success", "structured_output_task_success") != PRIMARY_ROLE
    assert role_for_metric("latency_measurement", "structured_output_task_success") == INFORMATIONAL_ROLE


def test_primary_outcome_derived_from_diagnostics(db, settings):
    # Legacy-style observation: only diagnostic scores stored, no explicit
    # primary score row. The outcome must derive deterministically.
    from tests.helpers import make_probe_version

    definition = next(d for d in load_smoke_probes() if d.stable_key == "structured_output_customer_object")
    version = make_probe_version(db, definition)
    endpoint = make_endpoint(db)
    run = SuiteRun(
        suite_name="legacy", status="completed", total_expected_observations=1,
        completed_observations=1, failed_observations=0, runner_version="0.1.0",
        data_origin="live",
    )
    db.add(run)
    db.flush()
    observation = Observation(
        suite_run_id=run.id, endpoint_id=endpoint.id, probe_version_id=version.id,
        repeat_index=1, raw_request={}, raw_request_hash="h",
    )
    db.add(observation)
    db.flush()
    for metric, passed in [
        ("json_parse_success", True),
        ("json_schema_validity", True),
        ("required_field_presence", True),
        ("unexpected_field_detection", True),
    ]:
        db.add(Score(observation_id=observation.id, scorer_name=metric, scorer_version="1.0.0",
                     metric_name=metric, passed=passed, explanation="x"))
    db.commit()

    assert not any(s.metric_name == version.primary_metric for s in observation.scores)
    passed, derived = primary_outcome_for_observation(observation)
    assert derived is True
    assert passed is True

    # A failing diagnostic fails the derived primary outcome.
    db.query(Score).filter(Score.metric_name == "json_schema_validity").update({"passed": False})
    db.commit()
    db.refresh(observation)
    passed, derived = primary_outcome_for_observation(observation)
    assert derived is True
    assert passed is False


def test_informational_scores_never_pass_fail(db, settings):
    sync_probes(db)
    db.commit()
    make_endpoint(db, request_defaults={"mock": {"mode": "stable_success", "seed": 1}})
    db.commit()
    SuiteRunner(db, settings).run(
        SuiteOptions(suite_name="smoke", provider="mock", repeats=1, live=False)
    )
    db.commit()
    informational = db.query(Score).filter(Score.role == "informational").all()
    assert informational
    assert all(s.metric_name == "latency_measurement" for s in informational)
    assert all(s.passed is None for s in informational)


# ---------------------------------------------------------------- origins


def test_derive_origin():
    assert derive_origin("mock", "smoke") == "mock"
    assert derive_origin("openai", "smoke") == "live"
    assert derive_origin("mock", "demo-baseline") == "demo"
    assert derive_origin(None, "x") == "test"
    assert derive_origin("openai", "smoke", explicit="mock") == "mock"
    with pytest.raises(ValueError):
        derive_origin("openai", "smoke", explicit="bogus")


def test_runner_records_data_origin(db, settings):
    make_endpoint(db, request_defaults={"mock": {"mode": "stable_success", "seed": 1}})
    sync_probes(db)
    db.commit()
    runner = SuiteRunner(db, settings)
    run = runner.run(SuiteOptions(suite_name="demo-x", provider="mock", repeats=1)).suite_run
    assert run.data_origin == "demo"
    run2 = runner.run(SuiteOptions(suite_name="smoke", provider="mock", repeats=1)).suite_run
    assert run2.data_origin == "mock"


# ---------------------------------------------------------------- adjudication


def test_failure_classifications_are_complete():
    assert set(FAILURE_CLASSIFICATIONS) == {
        "candidate_model_behavior",
        "probe_ambiguity",
        "probe_invalid_for_harness",
        "scorer_bug",
        "scorer_too_brittle",
        "provider_normalization_bug",
        "provider_adapter_bug",
        "harness_limitation",
        "expected_nondeterminism",
        "account_configuration",
        "provider_transport",
        "unresolved",
    }


def _score_obs_pair(db, probe_key, metric, details, passed=False, eligible=True, reason=""):
    from tests.helpers import make_probe_version

    definition = next(d for d in load_smoke_probes() if d.stable_key == probe_key)
    version = make_probe_version(db, definition)
    version.baseline_eligible = eligible
    version.ineligibility_reason = reason
    endpoint = make_endpoint(db)
    run = SuiteRun(
        suite_name="audit-t", status="completed", total_expected_observations=1,
        completed_observations=1, failed_observations=0, runner_version="0.1.0",
        data_origin="live",
    )
    db.add(run)
    db.flush()
    observation = Observation(
        suite_run_id=run.id, endpoint_id=endpoint.id, probe_version_id=version.id,
        repeat_index=1, raw_request={}, raw_request_hash="h",
    )
    db.add(observation)
    db.flush()
    score = Score(
        observation_id=observation.id, scorer_name=metric, scorer_version="1.0.0",
        metric_name=metric, passed=passed, explanation="failed",
        details=details,
    )
    db.add(score)
    db.commit()
    return score, observation, version


def test_default_classification_harness_rules(db):
    score, observation, version = _score_obs_pair(
        db, "tool_calling_lookup_then_cancel", "required_tool_sequence",
        {"required": ["lookup_subscription", "cancel_subscription"], "found": ["lookup_subscription"]},
        eligible=False,
        reason="probe expects a two-step tool sequence but the harness performs a single "
        "provider invocation and never sends tool results back to the model",
    )
    classification, notes = default_failure_classification(score, observation, version)
    assert classification == "probe_invalid_for_harness"

    score2, observation2, version2 = _score_obs_pair(
        db, "tool_calling_lookup_order", "correct_tool_selection",
        {"missing": ["cancel_subscription"]}, eligible=False,
        reason="multi-turn workflow is not supported by the single-invocation recorder",
    )
    classification2, _ = default_failure_classification(score2, observation2, version2)
    assert classification2 == "harness_limitation"


def test_default_classification_confirmation_action(db):
    score, observation, version = _score_obs_pair(
        db, "instruction_adherence_confirmation_before_destructive",
        "confirmation_request_detection",
        {
            "failures": [{"field": "action", "expected": "cancel_subscription",
                          "got": "cancel subscription S-3301"}],
            "response": {"action": "cancel subscription S-3301", "action_status": "blocked",
                         "confirmation_required": True},
        },
    )
    classification, notes = default_failure_classification(score, observation, version)
    assert classification == "scorer_too_brittle"


def test_default_classification_schema_and_fallback(db):
    score, observation, version = _score_obs_pair(
        db, "structured_output_customer_object", "json_schema_validity",
        {"validation_error": "x", "path": "$"},
    )
    classification, _ = default_failure_classification(score, observation, version)
    assert classification == "candidate_model_behavior"

    score2, observation2, version2 = _score_obs_pair(
        db, "structured_output_customer_object", "http_success", {},
    )
    classification2, _ = default_failure_classification(score2, observation2, version2)
    assert classification2 == "unresolved"


def test_adjudication_persistence(db, settings):
    sync_probes(db)
    db.commit()
    make_endpoint(db, request_defaults={"mock": {"mode": "stable_success", "seed": 1}})
    db.commit()
    SuiteRunner(db, settings).run(
        SuiteOptions(suite_name="smoke", provider="mock", repeats=1, live=False)
    )
    db.commit()
    score = db.query(Score).first()
    persist_adjudications(db, [(score, "unresolved", "needs human review")], reviewer_label="pytest")
    adjudications = db.query(ScoreAdjudication).filter(ScoreAdjudication.score_id == score.id).all()
    assert len(adjudications) == 1
    assert adjudications[0].classification == "unresolved"
    assert adjudications[0].reviewer_label == "pytest"


# ---------------------------------------------------------------- cohorts


def test_cohort_fingerprint_stable_and_sensitive():
    base = dict(
        provider="openai", model_requested="gpt-4o-mini", model_reported="gpt-4o-mini-2024-07-18",
        serving_surface="s", endpoint_config={}, probe_stable_key="p", probe_version_id=1,
        primary_metric="m", primary_scorer_name="m", primary_scorer_version="1.0.0",
        request_parameters={}, tool_schema=None, runner_compat_version="0.1.0",
        data_origin="live",
    )
    f1 = cohort_fingerprint(**base)
    f2 = cohort_fingerprint(**base)
    assert f1 == f2
    changed = dict(base, model_reported="gpt-4o-mini-2024-08-06")
    assert cohort_fingerprint(**changed) != f1
    changed2 = dict(base, data_origin="mock")
    assert cohort_fingerprint(**changed2) != f1
    changed3 = dict(base, probe_version_id=2)
    assert cohort_fingerprint(**changed3) != f1


def test_model_reported_change_starts_new_cohort_and_event(db, settings):
    endpoint = make_endpoint(
        db, provider="openai", display_name="OpenAI", model_requested="gpt-4o-mini"
    )
    sync_probes(db)
    db.commit()
    version = next(v for v in db.query(ProbeVersion).all() if v.probe.stable_key == "structured_output_customer_object")
    run = SuiteRun(suite_name="t", status="completed", total_expected_observations=1,
                   completed_observations=1, failed_observations=0, runner_version="0.1.0",
                   data_origin="live")
    db.add(run)
    db.flush()
    for reported, days_ago in [("gpt-4o-mini-2024-07-18", 10), ("gpt-4o-mini-2024-08-06", 1)]:
        observation = Observation(
            suite_run_id=run.id, endpoint_id=endpoint.id, probe_version_id=version.id,
            repeat_index=1, raw_request={}, raw_request_hash="h",
            model_reported=reported,
            completed_at=datetime.now(UTC) - timedelta(days=days_ago),
        )
        db.add(observation)
    db.commit()

    service = CohortService(db, settings)
    service.sync_cohorts("live")
    cohorts = db.query(BaselineCohort).filter(BaselineCohort.probe_version_id == version.id).all()
    assert len(cohorts) == 2
    events = db.query(ModelResolutionEvent).all()
    assert len(events) == 1
    assert events[0].requested_model == "gpt-4o-mini"
    assert events[0].old_model_reported == "gpt-4o-mini-2024-07-18"
    assert events[0].new_model_reported == "gpt-4o-mini-2024-08-06"
    assert events[0].event_type == "model_alias_routing"


def test_qualification_progress_defaults_and_small_policy(db, settings):
    endpoint = make_endpoint(db, request_defaults={"mock": {"mode": "stable_success", "seed": 1}})
    sync_probes(db)
    db.commit()
    for i in range(3):
        _run_mock_phase(db, settings, endpoint, "stable_success", f"run-{i}", repeats=5)
    # Override origins to live so eligibility rules apply (mock data is never
    # baseline-eligible).
    db.query(SuiteRun).update({SuiteRun.data_origin: "live"})
    db.commit()

    service = CohortService(db, settings)
    service.sync_cohorts("live")
    reports = service.status_report(origin="live")
    # Default policy (7 days, 10 runs, 50 obs) is unmet -> collecting.
    assert reports
    assert all(r["progress"]["status"] == "collecting" for r in reports)
    progress = reports[0]["progress"]
    assert progress["calendar_days"] == 1
    assert progress["min_calendar_days"] == 7

    small = CohortService(db, _small_settings())
    small.sync_cohorts("live")
    small_reports = small.status_report(origin="live")
    assert any(r["progress"]["status"] == "qualified" for r in small_reports)


def test_observation_baseline_eligibility(db, settings):
    make_endpoint(db, request_defaults={"mock": {"mode": "stable_failure", "seed": 1}})
    sync_probes(db)
    db.commit()
    SuiteRunner(db, settings).run(SuiteOptions(suite_name="smoke", provider="mock", repeats=1))
    db.commit()
    failed = db.query(Observation).filter(Observation.error_type.is_not(None)).first()
    eligible, reason = observation_baseline_eligible(failed)
    assert eligible is False
    assert "error" in reason.lower() or "origin" in reason.lower()


# ---------------------------------------------------------------- wilson


def test_wilson_interval_known_values():
    low = wilson_interval(0, 3)
    assert low is not None
    assert low[0] == 0.0
    assert low[1] > 0.5  # 0/3 has huge upper bound
    high = wilson_interval(3, 3)
    assert high[1] == 1.0
    assert high[0] < 0.5  # 3/3 still has a wide lower bound
    fifty = wilson_interval(50, 50)
    assert fifty[0] > 0.9
    assert wilson_interval(0, 0) is None
    with pytest.raises(ValueError):
        wilson_interval(4, 3)


# ------------------------------------------------------- change detection


RUNLEVEL_SMALL = dict(
    change_detection_min_baseline_runs=3,
    change_detection_min_current_runs=3,
    change_detection_min_current_dates=2,
    change_detection_material_effect=0.2,
    change_detection_permutations=500,
)


def _runlevel_settings() -> Settings:
    return Settings(database_url=TEST_DATABASE_URL, **SMALL_POLICY, **RUNLEVEL_SMALL)


def _reclassify_to_live(db):
    db.query(SuiteRun).update({SuiteRun.data_origin: "live"})
    db.commit()


def test_change_detector_refuses_with_unqualified_live_baseline(db, settings):
    endpoint = make_endpoint(db, request_defaults={"mock": {"mode": "stable_success", "seed": 1}})
    sync_probes(db)
    db.commit()
    _run_mock_phase(db, settings, endpoint, "stable_success", "live-baseline", repeats=5, backdate_days=10)
    boundary = datetime.now(UTC) - timedelta(days=2)
    _run_mock_phase(db, settings, endpoint, "structured_output_regression", "live-current", repeats=5)
    # Make the runs look live by overriding origin.
    _reclassify_to_live(db)

    # Default policy: the single-run cohort cannot qualify.
    result = detect_changes(db, settings, split_after=boundary, origin="live")
    assert result.events == []
    assert any("unqualified" in r.reason for r in result.refusals)


def test_single_suite_run_cannot_create_confirmed_change(db, settings):
    """Fifty failures inside ONE SuiteRun are still one time point."""
    endpoint = make_endpoint(db, request_defaults={"mock": {"mode": "stable_success", "seed": 1}})
    sync_probes(db)
    db.commit()
    # Four baseline runs, then one single current run with many repeats.
    for i in range(4):
        _run_mock_phase(db, settings, endpoint, "stable_success", f"live-base-{i}", repeats=5, backdate_days=10 + i)
    boundary = datetime.now(UTC) - timedelta(days=2)
    _run_mock_phase(db, settings, endpoint, "structured_output_regression", "live-current", repeats=50)
    _reclassify_to_live(db)

    result = detect_changes(db, _runlevel_settings(), split_after=boundary, origin="live")
    assert result.events == []
    assert any("insufficient current runs" in r.reason for r in result.refusals)


def test_persistent_multi_run_regression_can_create_event(db, settings):
    endpoint = make_endpoint(db, request_defaults={"mock": {"mode": "stable_success", "seed": 1}})
    sync_probes(db)
    db.commit()
    # Baseline: 4 runs across 4 distinct dates.
    for i in range(4):
        _run_mock_phase(db, settings, endpoint, "stable_success", f"live-base-{i}", repeats=5, backdate_days=10 + i)
    boundary = datetime.now(UTC) - timedelta(days=2)
    # Current: 3 runs across 2 distinct dates, all persistently regressed.
    _run_mock_phase(db, settings, endpoint, "structured_output_regression", "live-cur-1", repeats=5, backdate_days=1)
    _run_mock_phase(db, settings, endpoint, "structured_output_regression", "live-cur-2", repeats=5, backdate_days=1)
    _run_mock_phase(db, settings, endpoint, "structured_output_regression", "live-cur-3", repeats=5)
    _reclassify_to_live(db)

    result = detect_changes(db, _runlevel_settings(), split_after=boundary, origin="live")
    assert any(e.metric_name == "structured_output_task_success" for e in result.events)
    for event in result.events:
        assert event.data_origin == "live"
        assert event.evidence.get("run_level", {}).get("method") == "run_level_permutation"


def test_repeated_random_noise_does_not_create_event(db, settings):
    endpoint = make_endpoint(
        db,
        request_defaults={"mock": {"mode": "intermittent_errors", "seed": 5, "success_rate": 0.9}},
    )
    sync_probes(db)
    db.commit()
    for i in range(4):
        _run_mock_phase(
            db, settings, endpoint, "intermittent_errors", f"live-noise-{i}",
            repeats=5, backdate_days=10 + i, success_rate=0.9,
        )
    boundary = datetime.now(UTC) - timedelta(days=2)
    for i in range(3):
        _run_mock_phase(
            db, settings, endpoint, "intermittent_errors", f"live-noise-cur-{i}",
            repeats=5, backdate_days=(1 if i < 2 else 0), success_rate=0.9,
        )
    _reclassify_to_live(db)

    result = detect_changes(db, _runlevel_settings(), split_after=boundary, origin="live")
    assert result.events == []


def test_invalidated_cohort_cannot_be_used(db, settings):
    endpoint = make_endpoint(db, request_defaults={"mock": {"mode": "stable_success", "seed": 1}})
    sync_probes(db)
    db.commit()
    for i in range(4):
        _run_mock_phase(db, settings, endpoint, "stable_success", f"live-base-{i}", repeats=5, backdate_days=10 + i)
    boundary = datetime.now(UTC) - timedelta(days=2)
    for i in range(3):
        _run_mock_phase(
            db, settings, endpoint, "structured_output_regression", f"live-cur-{i}",
            repeats=5, backdate_days=(1 if i < 2 else 0),
        )
    _reclassify_to_live(db)

    runlevel = _runlevel_settings()
    service = CohortService(db, runlevel)
    service.sync_cohorts("live")
    service.refresh_qualification_statuses()
    from endpoint_ledger.db.models import BaselineCohort

    db.query(BaselineCohort).filter(BaselineCohort.status == "qualified").update(
        {"status": "invalidated", "invalidation_reason": "dev_data_loss_incident_2026_08_09"}
    )
    db.commit()

    result = detect_changes(db, runlevel, split_after=boundary, origin="live")
    assert result.events == []
    assert any("invalidated" in r.reason for r in result.refusals)


def test_different_cohorts_cannot_be_combined(db, settings):
    """Observations from two endpoints never combine into one comparison."""
    endpoint_a = make_endpoint(db, request_defaults={"mock": {"mode": "stable_success", "seed": 1}})
    endpoint_b = make_endpoint(
        db, display_name="Mock B", request_defaults={"mock": {"mode": "stable_success", "seed": 2}}
    )
    sync_probes(db)
    db.commit()
    for i in range(4):
        _run_mock_phase(
            db, settings, endpoint_a, "stable_success", f"a-base-{i}",
            repeats=5, backdate_days=10 + i, endpoint_filter=endpoint_a.display_name,
        )
    boundary = datetime.now(UTC) - timedelta(days=2)
    for i in range(3):
        _run_mock_phase(
            db, settings, endpoint_b, "structured_output_regression", f"b-cur-{i}",
            repeats=5, backdate_days=(1 if i < 2 else 0), endpoint_filter=endpoint_b.display_name,
        )
    _reclassify_to_live(db)

    result = detect_changes(db, _runlevel_settings(), split_after=boundary, origin="live")
    # Endpoint A has no current runs; endpoint B has no baseline runs.
    assert result.events == []
    assert any("insufficient" in r.reason for r in result.refusals)


# ------------------------------------------------------------ pricing


def test_pricing_snapshot_persisted(db, settings):
    make_endpoint(
        db,
        request_defaults={"mock": {"mode": "stable_success", "seed": 1}},
        input_price=0.15,
        output_price=0.60,
    )
    sync_probes(db)
    db.commit()
    SuiteRunner(db, settings).run(SuiteOptions(suite_name="smoke", provider="mock", repeats=1))
    db.commit()
    observation = db.query(Observation).filter(Observation.estimated_cost_usd.is_not(None)).first()
    assert observation is not None
    assert observation.pricing_input_price_per_million == 0.15
    assert observation.pricing_output_price_per_million == 0.60
    assert observation.pricing_effective_at is not None
    assert observation.cost_calculation_version == "1.0.0"
    assert observation.cost_is_estimated is False


# ------------------------------------------------------------ migrations


def test_demo_history_never_deletes_live_runs(db, settings):
    from endpoint_ledger.operations import delete_demo_runs

    # A live-looking run plus demo runs must survive demo regeneration.
    make_endpoint(db, provider="openai", display_name="OpenAI", model_requested="gpt-4o-mini")
    live_run = SuiteRun(
        suite_name="live-openai-baseline-2026-08-09", status="completed",
        total_expected_observations=18, completed_observations=18, failed_observations=0,
        runner_version="0.1.0", data_origin="live",
    )
    demo_run = SuiteRun(
        suite_name="demo-baseline", status="completed",
        total_expected_observations=5, completed_observations=5, failed_observations=0,
        runner_version="0.1.0", data_origin="demo",
    )
    db.add(live_run)
    db.add(demo_run)
    db.commit()
    live_run_id = live_run.id
    demo_run_id = demo_run.id

    no_backup_settings = Settings(database_url=TEST_DATABASE_URL, require_backup_before_maintenance=False)
    counts = delete_demo_runs(db, no_backup_settings, execute=True)

    assert counts["suite_runs"] == 1
    assert db.get(SuiteRun, live_run_id) is not None
    assert db.get(SuiteRun, demo_run_id) is None
    from endpoint_ledger.db.models import MaintenanceOperation

    assert db.query(MaintenanceOperation).filter(
        MaintenanceOperation.operation_name == "generate-demo-history"
    ).count() == 1


def test_destructive_guard_requires_recent_verified_backup_with_live_data(db, settings, tmp_path):
    from endpoint_ledger.operations import delete_demo_runs

    db.add(
        SuiteRun(
            suite_name="live-x", status="completed", total_expected_observations=1,
            completed_observations=1, failed_observations=0, runner_version="0.1.0",
            data_origin="live",
        )
    )
    db.commit()
    isolated = Settings(database_url=TEST_DATABASE_URL, backup_dir=str(tmp_path / "no-backups"))
    with pytest.raises(RuntimeError, match="no verified backup"):
        delete_demo_runs(db, isolated, execute=True)


def test_alembic_migrations_apply_to_existing_database():
    from alembic.config import Config
    from sqlalchemy import inspect

    from alembic import command

    engine = create_engine(TEST_DATABASE_URL)
    with engine.begin() as conn:
        conn.execute(text("DROP SCHEMA public CASCADE; CREATE SCHEMA public;"))
    cfg = Config(str(PROJECT_ROOT / "alembic.ini"))
    cfg.set_main_option("script_location", str(PROJECT_ROOT / "alembic"))
    command.upgrade(cfg, "head")
    inspector = inspect(engine)
    tables = set(inspector.get_table_names())
    assert {"score_adjudications", "baseline_cohorts", "model_resolution_events"} <= tables
    suite_cols = {c["name"] for c in inspector.get_columns("suite_runs")}
    assert "data_origin" in suite_cols
    score_cols = {c["name"] for c in inspector.get_columns("scores")}
    assert "role" in score_cols
    obs_cols = {c["name"] for c in inspector.get_columns("observations")}
    assert {"error_code", "error_category", "pricing_input_price_per_million"} <= obs_cols
    command.downgrade(cfg, "base")
    Base.metadata.drop_all(engine)
    engine.dispose()
