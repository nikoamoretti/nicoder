"""Change detection tests: noise never triggers; persistent regression does."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

from endpoint_ledger.change_detection.service import (
    WindowSample,
    compare_binary,
    compare_latency,
    detect_changes,
)
from endpoint_ledger.config import Settings
from endpoint_ledger.runner.service import SuiteOptions, SuiteRunner
from tests.helpers import make_endpoint, sync_probes


def _ws(passed: bool) -> WindowSample:
    return WindowSample(passed=passed, numeric_value=None, completed_at=datetime.now(UTC))


def _ws_num(value: float) -> WindowSample:
    return WindowSample(passed=None, numeric_value=value, completed_at=datetime.now(UTC))


MIN_OBS = 5
ABS = 0.25
ALPHA = 0.05


def test_random_noise_does_not_produce_change_event():
    baseline = [_ws(True)] * 8 + [_ws(False)] * 2  # 80% pass
    current = [_ws(False)] + [_ws(True)] * 8  # ~89% pass
    comparison = compare_binary("m", None, baseline, current, MIN_OBS, ABS, ALPHA)
    assert comparison.qualifies is False
    assert "no material change" in comparison.reason


def test_persistent_regression_produces_event():
    baseline = [_ws(True)] * 10
    current = [_ws(False)] * 10
    comparison = compare_binary("m", None, baseline, current, MIN_OBS, ABS, ALPHA)
    assert comparison.qualifies is True
    assert comparison.direction == "regression"
    assert comparison.baseline_value == 1.0
    assert comparison.current_value == 0.0
    assert comparison.p_value is not None and comparison.p_value < ALPHA


def test_improvement_detected():
    baseline = [_ws(False)] * 10
    current = [_ws(True)] * 10
    comparison = compare_binary("m", None, baseline, current, MIN_OBS, ABS, ALPHA)
    assert comparison.qualifies is True
    assert comparison.direction == "improvement"


def test_insufficient_samples_never_qualifies():
    baseline = [_ws(True)] * 2
    current = [_ws(False)] * 10
    comparison = compare_binary("m", None, baseline, current, MIN_OBS, ABS, ALPHA)
    assert comparison.qualifies is False
    assert "insufficient samples" in comparison.reason


def test_threshold_respected():
    baseline = [_ws(True)] * 10
    current = [_ws(True)] * 8 + [_ws(False)] * 2  # 20% change < 25% threshold
    comparison = compare_binary("m", None, baseline, current, MIN_OBS, ABS, ALPHA)
    assert comparison.qualifies is False


def test_latency_comparison():
    baseline = [_ws_num(100.0 + i) for i in range(10)]
    current = [_ws_num(2000.0 + i) for i in range(10)]
    comparison = compare_latency("latency_measurement", None, baseline, current, 5, 500, ALPHA)
    assert comparison.qualifies is True
    assert comparison.direction == "slower"
    assert comparison.baseline_value < comparison.current_value


def test_latency_requires_minimum_samples():
    baseline = [_ws_num(100.0)] * 2
    current = [_ws_num(5000.0)] * 2
    comparison = compare_latency("latency_measurement", None, baseline, current, 5, 500, ALPHA)
    assert comparison.qualifies is False
    assert "insufficient samples" in comparison.reason


def _run_phase(
    session,
    endpoint,
    suite_name: str,
    mode: str,
    settings: Settings,
    backdate_days: int | None,
    success_rate: float = 1.0,
    repeats: int = 5,
):
    endpoint.request_defaults = {"mock": {"mode": mode, "seed": 5, "success_rate": success_rate}}
    session.commit()
    runner = SuiteRunner(session, settings)
    summary = runner.run(
        SuiteOptions(suite_name=suite_name, provider="mock", repeats=repeats, live=False)
    )
    run = summary.suite_run
    assert run is not None
    if backdate_days:
        offset = timedelta(days=backdate_days)
        run.started_at = run.started_at - offset
        run.completed_at = (run.completed_at or run.started_at) - offset
        for observation in run.observations:
            observation.requested_at = observation.requested_at - offset
            if observation.completed_at is not None:
                observation.completed_at = observation.completed_at - offset
        session.commit()
    return run


def test_persistent_regression_detected_in_db(db, settings):
    endpoint = make_endpoint(
        db, request_defaults={"mock": {"mode": "stable_success", "seed": 5}}
    )
    sync_probes(db)
    db.commit()

    baseline_run = _run_phase(db, endpoint, "baseline", "stable_success", settings, backdate_days=30)
    boundary = max(o.completed_at or o.requested_at for o in baseline_run.observations)

    # The provider now persistently regresses on structured output.
    _run_phase(db, endpoint, "regression", "structured_output_regression", settings, backdate_days=None)

    # Mock runs are origin=mock; non-live detection is explicit and never
    # produces a live behavioral claim.
    result = detect_changes(
        db, settings, split_after=boundary,
        origin="mock",  # type: ignore[arg-type]
        require_qualified_cohort=False,
    )
    events = result.events
    assert events, "persistent regression should produce at least one change event"
    metric_names = {e.metric_name for e in events}
    assert "structured_output_task_success" in metric_names
    assert all(e.status == "provisional" for e in events)
    assert all(e.direction == "regression" for e in events)
    assert all(e.data_origin == "mock" for e in events)


def test_noise_does_not_create_db_event(db, settings):
    # Same seeded intermittent behavior in both windows — no regime change.
    endpoint = make_endpoint(
        db,
        request_defaults={"mock": {"mode": "intermittent_errors", "seed": 5, "success_rate": 0.9}},
    )
    sync_probes(db)
    db.commit()

    baseline_run = _run_phase(
        db, endpoint, "baseline-noise", "intermittent_errors", settings,
        backdate_days=30, success_rate=0.9,
    )
    boundary = max(o.completed_at or o.requested_at for o in baseline_run.observations)
    _run_phase(
        db, endpoint, "current-noise", "intermittent_errors", settings,
        backdate_days=None, success_rate=0.9,
    )

    result = detect_changes(
        db, settings, split_after=boundary,
        origin="mock",  # type: ignore[arg-type]
        require_qualified_cohort=False,
    )
    assert result.events == []
