"""Runner behavior tests: continuation after failure, suite status, cost ceiling."""

from __future__ import annotations

import pytest

from endpoint_ledger.db.models import Observation, Score, SuiteRun
from endpoint_ledger.runner.service import (
    CostCeilingExceededError,
    SuiteOptions,
    SuiteRunner,
)
from tests.helpers import make_endpoint, sync_probes


def _runner(db, settings):
    return SuiteRunner(db, settings)


def test_mock_suite_completes_with_all_observations(db, settings):
    make_endpoint(db, request_defaults={"mock": {"mode": "stable_success", "seed": 1}})
    sync_probes(db)
    db.commit()

    runner = _runner(db, settings)
    summary = runner.run(SuiteOptions(suite_name="smoke", provider="mock", repeats=3, live=False))
    run = summary.suite_run
    assert run is not None
    assert run.status == "completed"
    assert run.completed_observations == 18 == run.total_expected_observations
    assert run.failed_observations == 0
    assert db.query(Observation).count() == 18
    assert db.query(Score).count() > 0
    assert run.estimated_cost_usd is None  # pricing not configured -> unknown


def test_run_continues_after_individual_failures(db, settings):
    make_endpoint(
        db,
        request_defaults={"mock": {"mode": "intermittent_errors", "seed": 2, "success_rate": 0.35}},
    )
    sync_probes(db)
    db.commit()

    runner = _runner(db, settings)
    summary = runner.run(SuiteOptions(suite_name="flaky", provider="mock", repeats=3, live=False))
    run = summary.suite_run
    assert run is not None
    # All observations saved even though some requests failed.
    assert run.completed_observations == run.total_expected_observations
    assert db.query(Observation).count() == run.total_expected_observations
    assert run.failed_observations > 0
    assert run.status == "completed_with_errors"
    failures = db.query(Observation).filter(Observation.error_type.is_not(None)).all()
    successes = db.query(Observation).filter(Observation.error_type.is_(None)).all()
    assert failures and successes


def test_dry_run_calls_no_apis_and_persists_nothing(db, settings):
    make_endpoint(db, request_defaults={"mock": {"mode": "stable_failure", "seed": 1}})
    sync_probes(db)
    db.commit()

    runner = _runner(db, settings)
    summary = runner.run(SuiteOptions(suite_name="smoke", provider="mock", repeats=3, dry_run=True))
    assert summary.suite_run is None
    assert summary.planned_observations == 18
    assert db.query(SuiteRun).count() == 0
    assert db.query(Observation).count() == 0


def test_openai_requires_live_flag(db, settings):
    make_endpoint(db, provider="openai", display_name="OpenAI", model_requested="gpt-4o-mini")
    sync_probes(db)
    db.commit()

    runner = _runner(db, settings)
    summary = runner.run(SuiteOptions(suite_name="smoke", provider="openai", live=False))
    run = summary.suite_run
    assert run is not None
    assert run.status == "failed"
    assert run.completed_observations == 0
    assert "live provider" in run.error_summary


def test_openai_unavailable_without_key(db, settings, monkeypatch):
    monkeypatch.delenv("OPENAI_API_KEY", raising=False)
    monkeypatch.setenv("OPENAI_API_KEY", "")
    make_endpoint(db, provider="openai", display_name="OpenAI", model_requested="gpt-4o-mini")
    sync_probes(db)
    db.commit()

    runner = _runner(db, settings)
    summary = runner.run(SuiteOptions(suite_name="smoke", provider="openai", live=True))
    run = summary.suite_run
    assert run is not None
    assert run.status == "failed"
    assert "credential" in run.error_summary
    assert run.completed_observations == 0


def test_cost_ceiling_refuses_expensive_run(db, settings):
    make_endpoint(
        db,
        request_defaults={"mock": {"mode": "stable_success", "seed": 1}},
        input_price=100.0,
        output_price=200.0,
    )
    sync_probes(db)
    db.commit()

    runner = _runner(db, settings)
    options = SuiteOptions(
        suite_name="smoke", provider="mock", repeats=3, cost_ceiling_usd=0.0001
    )
    with pytest.raises(CostCeilingExceededError, match="exceeds ceiling"):
        runner.run(options)
    assert db.query(SuiteRun).count() == 0


def test_cost_ceiling_allows_cheap_run(db, settings):
    make_endpoint(
        db,
        request_defaults={"mock": {"mode": "stable_success", "seed": 1}},
        input_price=0.5,
        output_price=1.5,
    )
    sync_probes(db)
    db.commit()

    runner = _runner(db, settings)
    summary = runner.run(
        SuiteOptions(suite_name="smoke", provider="mock", repeats=3, cost_ceiling_usd=100.0)
    )
    run = summary.suite_run
    assert run is not None
    assert run.status == "completed"
    assert run.estimated_cost_usd is not None and run.estimated_cost_usd > 0
    assert run.actual_cost_usd is not None


def test_unknown_pricing_not_invented(db, settings):
    make_endpoint(db, request_defaults={"mock": {"mode": "stable_success", "seed": 1}})
    sync_probes(db)
    db.commit()

    runner = _runner(db, settings)
    summary = runner.run(SuiteOptions(suite_name="smoke", provider="mock", repeats=3))
    run = summary.suite_run
    assert run is not None
    assert run.estimated_cost_usd is None
    assert run.actual_cost_usd is None
    # ceiling does not block when cost is unknown
    summary2 = runner.run(
        SuiteOptions(suite_name="smoke", provider="mock", repeats=3, cost_ceiling_usd=0.0001)
    )
    assert summary2.suite_run.status == "completed"


def test_status_failed_when_no_matching_endpoints(db, settings):
    sync_probes(db)
    db.commit()
    runner = _runner(db, settings)
    with pytest.raises(ValueError, match="no enabled endpoints"):
        runner.run(SuiteOptions(suite_name="smoke", provider="mock", repeats=3))


def test_repeat_count_respected(db, settings):
    make_endpoint(db, request_defaults={"mock": {"mode": "stable_success", "seed": 1}})
    sync_probes(db)
    db.commit()
    runner = _runner(db, settings)
    summary = runner.run(SuiteOptions(suite_name="smoke", provider="mock", repeats=2, live=False))
    assert summary.planned_observations == 12
