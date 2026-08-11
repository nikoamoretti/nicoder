"""Phase 1.3 run-now tests: "Run One Additional Collection Now".

All provider interaction is mocked; no live API request is made anywhere in
this file. A verified-backup fixture is patched so the backup requirement is
satisfied deterministically without touching the real backup directory.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import text

from endpoint_ledger.config import Settings
from endpoint_ledger.db.models import (
    AutomationNotification,
    BaselineCohort,
    BaselineReport,
    CollectionPlan,
    ModelResolutionEvent,
    PlannedCollectionRun,
    PreflightRecord,
    RunArtifact,
    ScoreAdjudication,
    SuiteRun,
)
from endpoint_ledger.measurement.adjudication import MEASUREMENT_DEFECT_CLASSES
from endpoint_ledger.services.collection_service import (
    plan_cumulative_request_count,
    request_manual_run,
)
from endpoint_ledger.services.qualification_service import (
    approve as approve_run,
)
from endpoint_ledger.services.qualification_service import (
    execute_qualification_smoke,
)
from endpoint_ledger.web.app import create_app
from tests.conftest import TEST_DATABASE_URL
from tests.helpers import make_endpoint, sync_probes

FAKE_MODEL_REPORTED = "gpt-4o-mini-2024-07-18"
SHIFTED_MODEL_REPORTED = "gpt-4o-mini-2024-08-06"


def _make_openai_endpoint(db):
    return make_endpoint(
        db, provider="openai", display_name="OpenAI", model_requested="gpt-4o-mini",
        input_price=0.15, output_price=0.60,
    )


class FakeOpenAIAdapter:
    provider_name = "openai"

    def __init__(self):
        self.invoke_count = 0

    def is_available(self):
        return True

    def invoke(self, endpoint_config, probe_version, repeat_index):
        import json
        from datetime import datetime as dt

        from endpoint_ledger.runner.canonical_response import (
            CanonicalProviderResponse,
            ToolCall,
            Usage,
        )

        self.invoke_count += 1
        eb = probe_version.expected_behavior
        category = probe_version.probe.category if probe_version.probe else ""
        started = dt.now(UTC)
        text = None
        tool_calls = []
        if category == "tool_calling":
            for call in eb.get("example_tool_calls") or []:
                tool_calls.append(ToolCall(id=call.get("id"), name=call["name"], arguments=call.get("arguments")))
        else:
            text = json.dumps(eb.get("example"), sort_keys=True)
        completed = dt.now(UTC)
        return CanonicalProviderResponse(
            provider="openai",
            model_requested=endpoint_config.model_requested,
            model_reported=FAKE_MODEL_REPORTED,
            provider_request_id=f"chatcmpl-fake-{probe_version.id}-{repeat_index}",
            request_started_at=started,
            request_completed_at=completed,
            latency_ms=42,
            http_status=200,
            finish_reason="stop",
            text=text,
            tool_calls=tool_calls,
            usage=Usage(input_tokens=100, output_tokens=50, total_tokens=150),
            raw_response={"fake": True},
        )


@pytest.fixture()
def fake_openai(monkeypatch):
    """All provider calls use the fake OpenAI adapter; no live requests."""
    monkeypatch.setenv("OPENAI_API_KEY", "sk-fake")
    import endpoint_ledger.providers as providers
    import endpoint_ledger.runner.service as runner_mod

    fake = FakeOpenAIAdapter()

    def fake_get_provider(name, settings=None, session=None):
        if name == "openai":
            return fake
        from endpoint_ledger.providers.mock import MockProvider

        return MockProvider()

    monkeypatch.setattr(providers, "get_provider", fake_get_provider)
    monkeypatch.setattr(runner_mod, "get_provider", fake_get_provider)
    yield fake


@pytest.fixture()
def verified_backup(monkeypatch, tmp_path):
    """A deterministic, recent verified backup path (never touches .backups/)."""
    fake = tmp_path / "endpoint_ledger_fake.dump"
    fake.write_text("fake")
    sidecar = tmp_path / "endpoint_ledger_fake.dump.verified.json"
    sidecar.write_text('{"verified_at": "2026-08-10T00:00:00+00:00", "checks": []}')
    monkeypatch.setattr(
        "endpoint_ledger.backup.last_verified_backup",
        lambda settings=None: fake,
    )
    yield fake


def _small_policy_settings():
    return Settings(
        database_url=TEST_DATABASE_URL,
        baseline_min_calendar_days=0,
        baseline_min_distinct_runs=2,
        baseline_min_primary_observations=8,
        baseline_min_distinct_dates=1,
        baseline_min_elapsed_hours=0.0,
    )


def _setup_plan(db, settings, *, endpoint=None, repeats=1, approved=True,
                state="enabled", **plan_kwargs):
    """Endpoint + probes + approved qualification smoke + collection plan."""
    endpoint = endpoint or _make_openai_endpoint(db)
    sync_probes(db)
    db.commit()
    smoke = execute_qualification_smoke(db, settings)
    if approved:
        approve_run(db, smoke["run_id"])
    from endpoint_ledger.measurement.cohorts import CohortService

    reports = CohortService(db, settings).status_report(origin="live")
    fingerprint = reports[0]["cohort"].fingerprint if reports else "fp"
    plan = create_plan_for_test(
        db, settings, endpoint, fingerprint, repeats, smoke["run_id"], **plan_kwargs
    )
    if state == "enabled":
        from endpoint_ledger.services.collection_service import enable_plan

        enable_plan(db, plan.id)
    elif state == "paused":
        from endpoint_ledger.services.collection_service import enable_plan, pause_plan

        enable_plan(db, plan.id)
        pause_plan(db, plan.id)
    return plan


def create_plan_for_test(db, settings, endpoint, fingerprint, repeats, smoke_run_id, **kwargs):
    from endpoint_ledger.services.collection_service import create_plan

    return create_plan(
        db, settings, provider="openai", endpoint_id=endpoint.id, suite_name="smoke",
        cohort_fingerprint=fingerprint, repeats_per_probe=repeats,
        qualification_run_id=smoke_run_id, **kwargs,
    )


def _enqueue(db, settings, plan, key="manual-key-1"):
    result = request_manual_run(db, settings, plan.id, key)
    assert result["ok"] is True
    return result


def _app(db_url=TEST_DATABASE_URL, **overrides):
    return TestClient(create_app(Settings(database_url=db_url, **overrides)))


def _csrf_headers(client: TestClient) -> dict:
    client.get("/")
    token = client.cookies.get("el_csrftoken")
    return {"x-csrf-token": token}


# ------------------------------------------------ visibility and behavior


def test_01_run_now_visible_for_active_plan(db, settings, fake_openai, verified_backup):
    plan = _setup_plan(db, settings)
    client = _app()
    body = client.get("/baseline-ops").text
    assert "Run One Additional Collection Now" in body
    assert f"/baseline-ops/plan/{plan.id}/manual-run" in body


def test_02_run_now_visible_for_paused_plan(db, settings, fake_openai, verified_backup):
    _setup_plan(db, settings, state="paused")
    client = _app()
    body = client.get("/baseline-ops").text
    assert "Run One Additional Collection Now" in body


def test_03_manual_run_does_not_resume_paused_plan(db, settings, fake_openai, verified_backup):
    plan = _setup_plan(db, settings, state="paused")
    _enqueue(db, settings, plan)
    from endpoint_ledger.services.scheduler import scheduler_tick

    scheduler_tick(db, settings)
    plan = db.get(CollectionPlan, plan.id)
    assert plan.state == "paused"


def test_04_manual_run_does_not_alter_next_scheduled_run(db, settings, fake_openai, verified_backup):
    plan = _setup_plan(db, settings, state="enabled")
    scheduled = db.query(PlannedCollectionRun).filter(
        PlannedCollectionRun.collection_plan_id == plan.id,
        PlannedCollectionRun.trigger_type == "scheduled",
    ).first()
    assert scheduled is not None
    scheduled_for = scheduled.scheduled_for
    _enqueue(db, settings, plan)
    from endpoint_ledger.services.scheduler import scheduler_tick

    scheduler_tick(db, settings)
    scheduled = db.get(PlannedCollectionRun, scheduled.id)
    assert scheduled.status == "planned"
    assert scheduled.scheduled_for == scheduled_for


def test_05_confirmation_page_displays_request_count_and_max_cost(
    db, settings, fake_openai, verified_backup
):
    plan = _setup_plan(db, settings, repeats=5)
    client = _app()
    body = client.get(f"/baseline-ops/plan/{plan.id}/manual-run").text
    assert "Expected live requests" in body
    assert "Conservative max incremental cost" in body
    assert ">30<" in body or "> 30 <" in body
    assert "$0." in body


def test_06_confirmation_requires_post_csrf_checkbox_idempotency(
    db, settings, fake_openai, verified_backup
):
    plan = _setup_plan(db, settings)
    client = _app()
    # CSRF missing -> 403.
    resp = client.post(f"/baseline-ops/plan/{plan.id}/manual-run",
                       data={"confirm": "run", "idempotency_token": "k1"})
    assert resp.status_code == 403
    headers = _csrf_headers(client)
    # Checkbox missing -> 400.
    resp = client.post(f"/baseline-ops/plan/{plan.id}/manual-run",
                       data={"csrf_token": headers["x-csrf-token"], "idempotency_token": "k1"})
    assert resp.status_code in (400, 422)
    # Idempotency token missing -> 400/422.
    resp = client.post(f"/baseline-ops/plan/{plan.id}/manual-run",
                       data={"csrf_token": headers["x-csrf-token"], "confirm": "run"})
    assert resp.status_code in (400, 422)
    # No PlannedCollectionRun was created by the invalid attempts.
    assert db.query(PlannedCollectionRun).filter(
        PlannedCollectionRun.trigger_type == "manual_run_now"
    ).count() == 0


def test_07_double_submission_creates_exactly_one_planned_run(
    db, settings, fake_openai, verified_backup
):
    plan = _setup_plan(db, settings)
    client = _app()
    headers = _csrf_headers(client)
    data = {"csrf_token": headers["x-csrf-token"], "confirm": "run", "idempotency_token": "dup-key"}
    first = client.post(f"/baseline-ops/plan/{plan.id}/manual-run", data=data, follow_redirects=False)
    assert first.status_code == 303
    second = client.post(f"/baseline-ops/plan/{plan.id}/manual-run", data=data, follow_redirects=False)
    assert second.status_code == 303
    assert first.headers["location"] == second.headers["location"]
    assert db.query(PlannedCollectionRun).filter(
        PlannedCollectionRun.trigger_type == "manual_run_now"
    ).count() == 1


def test_08_browser_refresh_does_not_create_another_run(
    db, settings, fake_openai, verified_backup
):
    plan = _setup_plan(db, settings)
    client = _app()
    headers = _csrf_headers(client)
    data = {"csrf_token": headers["x-csrf-token"], "confirm": "run", "idempotency_token": "refresh-key"}
    client.post(f"/baseline-ops/plan/{plan.id}/manual-run", data=data)
    # A refresh resubmits the identical form (same idempotency token).
    client.post(f"/baseline-ops/plan/{plan.id}/manual-run", data=data)
    assert db.query(PlannedCollectionRun).filter(
        PlannedCollectionRun.trigger_type == "manual_run_now"
    ).count() == 1


def test_09_scheduler_claim_prevents_duplicate_execution(
    db, settings, fake_openai, verified_backup
):
    plan = _setup_plan(db, settings)
    result = _enqueue(db, settings, plan)
    from endpoint_ledger.services.scheduler import scheduler_tick

    scheduler_tick(db, settings)
    planned = db.get(PlannedCollectionRun, result["planned_run_id"])
    assert planned.suite_run_id is not None
    runs_after_first = db.query(SuiteRun).count()
    # A second tick cannot execute the completed run again.
    scheduler_tick(db, settings)
    assert db.query(SuiteRun).count() == runs_after_first
    # A claimed (running) run is never re-picked by a tick.
    planned.status = "running"
    db.commit()
    scheduler_tick(db, settings)
    assert db.query(SuiteRun).count() == runs_after_first
    assert db.get(PlannedCollectionRun, planned.id).status == "running"


def test_10_active_execution_blocks_another_manual_run(
    db, settings, fake_openai, verified_backup
):
    plan = _setup_plan(db, settings)
    result = _enqueue(db, settings, plan, key="k-running")
    planned = db.get(PlannedCollectionRun, result["planned_run_id"])
    planned.status = "running"
    db.commit()
    blocked = request_manual_run(db, settings, plan.id, "k-running-2")
    assert blocked["ok"] is False
    assert any("currently executing" in r for r in blocked["blockers"])


def test_11_queued_manual_run_blocks_duplicate_manual_run(
    db, settings, fake_openai, verified_backup
):
    plan = _setup_plan(db, settings)
    _enqueue(db, settings, plan, key="k-queued")
    blocked = request_manual_run(db, settings, plan.id, "k-queued-2")
    assert blocked["ok"] is False
    assert any("already queued" in r for r in blocked["blockers"])
    assert db.query(PlannedCollectionRun).filter(
        PlannedCollectionRun.trigger_type == "manual_run_now"
    ).count() == 1


def test_12_fresh_preflight_executes_before_enqueueing(
    db, settings, fake_openai, verified_backup
):
    plan = _setup_plan(db, settings)
    before = db.query(PreflightRecord).count()
    result = _enqueue(db, settings, plan)
    after = db.query(PreflightRecord).count()
    assert after == before + 1
    latest = db.query(PreflightRecord).order_by(PreflightRecord.id.desc()).first()
    planned = db.get(PlannedCollectionRun, result["planned_run_id"])
    assert planned.configuration_hash == latest.configuration_hash


def test_13_failed_preflight_makes_zero_provider_calls(
    db, settings, fake_openai, verified_backup, monkeypatch
):
    plan = _setup_plan(db, settings)

    class _Unavailable:
        provider_name = "openai"

        def is_available(self):
            return False

        def invoke(self, *args, **kwargs):
            raise AssertionError("no provider request may occur")

    monkeypatch.delenv("OPENAI_API_KEY", raising=False)
    before = fake_openai.invoke_count
    blocked = request_manual_run(db, settings, plan.id, "k-nokey")
    assert blocked["ok"] is False
    assert fake_openai.invoke_count == before


def test_14_missing_credential_blocks_the_request(
    db, settings, fake_openai, verified_backup, monkeypatch
):
    plan = _setup_plan(db, settings)

    class _Unavailable:
        provider_name = "openai"

        def is_available(self):
            return False

        def invoke(self, *args, **kwargs):
            raise AssertionError("no provider request may occur")

    monkeypatch.delenv("OPENAI_API_KEY", raising=False)
    blocked = request_manual_run(db, settings, plan.id, "k-nokey-2")
    assert any("credential" in r for r in blocked["blockers"])
    assert db.query(PlannedCollectionRun).filter(
        PlannedCollectionRun.trigger_type == "manual_run_now"
    ).count() == 0


def test_15_unresolved_measurement_issue_blocks(
    db, settings, fake_openai, verified_backup
):
    plan = _setup_plan(db, settings)
    smoke = db.query(SuiteRun).filter(SuiteRun.suite_name.like("qualification-smoke-%")).first()
    from endpoint_ledger.db.models import Score

    score = db.query(Score).filter(Score.observation_id.in_(
        [o.id for o in smoke.observations]
    )).first()
    db.add(ScoreAdjudication(
        score_id=score.id,
        classification=sorted(MEASUREMENT_DEFECT_CLASSES)[0],
        notes="measurement defect for test",
        reviewer_label="test",
    ))
    db.commit()
    blocked = request_manual_run(db, settings, plan.id, "k-defect")
    assert blocked["ok"] is False
    assert any("measurement issue" in r for r in blocked["blockers"])


def test_16_unresolved_model_resolution_event_blocks(
    db, settings, fake_openai, verified_backup
):
    plan = _setup_plan(db, settings)
    db.add(ModelResolutionEvent(
        requested_model="gpt-4o-mini",
        old_model_reported=FAKE_MODEL_REPORTED,
        new_model_reported=SHIFTED_MODEL_REPORTED,
        first_observed_at=datetime.now(UTC),
        event_type="model_alias_routing",
        notes="test",
    ))
    db.commit()
    blocked = request_manual_run(db, settings, plan.id, "k-modelres")
    assert blocked["ok"] is False
    assert any("model-resolution" in r for r in blocked["blockers"])


def test_17_cost_limit_blocks(db, settings, fake_openai, verified_backup):
    plan = _setup_plan(db, settings, maximum_total_cost_usd=0.0)
    blocked = request_manual_run(db, settings, plan.id, "k-cost")
    assert blocked["ok"] is False
    assert any("cost" in r for r in blocked["blockers"])


def test_18_request_count_limit_blocks(db, settings, fake_openai, verified_backup):
    plan = _setup_plan(db, settings, maximum_request_count=1)
    blocked = request_manual_run(db, settings, plan.id, "k-req")
    assert blocked["ok"] is False
    assert any("request-count" in r for r in blocked["blockers"])


def test_19_maximum_plan_end_timestamp_blocks(db, settings, fake_openai, verified_backup):
    plan = _setup_plan(db, settings, maximum_days=0)
    plan.maximum_end_at = datetime.now(UTC) - timedelta(minutes=1)
    db.commit()
    blocked = request_manual_run(db, settings, plan.id, "k-end")
    assert blocked["ok"] is False
    assert any("end timestamp" in r for r in blocked["blockers"])


def test_20_disabled_completed_and_qualified_plans_block(
    db, settings, fake_openai, verified_backup
):
    plan = _setup_plan(db, settings, state="enabled")
    from endpoint_ledger.services.collection_service import disable_plan

    disable_plan(db, plan.id)
    blocked = request_manual_run(db, settings, plan.id, "k-disabled")
    assert blocked["ok"] is False
    assert any("plan state is" in r for r in blocked["blockers"])

    plan2 = _setup_plan(db, settings)
    plan2.state = "completed_qualified"
    plan2.stop_reason = "cohort qualified"
    db.commit()
    blocked2 = request_manual_run(db, settings, plan2.id, "k-qualified")
    assert blocked2["ok"] is False
    assert any("plan state is" in r for r in blocked2["blockers"])


def test_21_manual_run_uses_full_safe_lifecycle(db, settings, fake_openai, verified_backup):
    plan = _setup_plan(db, settings)
    result = _enqueue(db, settings, plan)
    from endpoint_ledger.services.scheduler import scheduler_tick

    scheduler_tick(db, settings)
    planned = db.get(PlannedCollectionRun, result["planned_run_id"])
    assert planned.status == "completed"
    run = db.get(SuiteRun, planned.suite_run_id)
    assert run is not None
    assert run.data_origin == "live"
    assert run.evidence_state == "exported"
    assert run.evidence_manifest_hash
    artifact = db.query(RunArtifact).filter(RunArtifact.suite_run_id == run.id).one()
    assert artifact.verification_status == "verified"
    categories = [n.category for n in db.query(AutomationNotification).all()]
    assert "manual_collection_requested" in categories
    assert "manual_collection_started" in categories
    assert "manual_collection_completed" in categories


def test_22_partial_provider_failure_preserves_successful_observations(
    db, settings, fake_openai, verified_backup, monkeypatch
):
    plan = _setup_plan(db, settings, repeats=2)
    fake = fake_openai

    def flaky_invoke(endpoint_config, probe_version, repeat_index):
        if repeat_index == 2:
            raise RuntimeError("transient provider failure")
        return fake.invoke(endpoint_config, probe_version, repeat_index)

    import endpoint_ledger.providers as providers
    import endpoint_ledger.runner.service as runner_mod

    def flaky_get_provider(name, settings=None, session=None):
        return _AdapterWith(fake, flaky_invoke)

    monkeypatch.setattr(providers, "get_provider", flaky_get_provider)
    monkeypatch.setattr(runner_mod, "get_provider", flaky_get_provider)
    result = _enqueue(db, settings, plan)
    from endpoint_ledger.services.scheduler import scheduler_tick

    scheduler_tick(db, settings)
    planned = db.get(PlannedCollectionRun, result["planned_run_id"])
    assert planned.status == "completed_with_errors"
    run = db.get(SuiteRun, planned.suite_run_id)
    assert run.completed_observations > 0
    assert run.failed_observations > 0
    assert run.evidence_state == "exported"
    categories = [n.category for n in db.query(AutomationNotification).all()]
    assert "manual_collection_completed_with_errors" in categories


class _AdapterWith:
    provider_name = "openai"

    def __init__(self, base, invoke):
        self._base = base
        self._invoke = invoke

    def is_available(self):
        return True

    def invoke(self, *args, **kwargs):
        return self._invoke(*args, **kwargs)


def test_23_export_failure_preserves_sealed_database_evidence(
    db, settings, fake_openai, verified_backup, monkeypatch
):
    plan = _setup_plan(db, settings)
    result = _enqueue(db, settings, plan)

    def boom(*args, **kwargs):
        raise RuntimeError("export disk failure")

    monkeypatch.setattr("endpoint_ledger.services.evidence_service.export_run", boom)
    from endpoint_ledger.services.scheduler import scheduler_tick

    scheduler_tick(db, settings)
    planned = db.get(PlannedCollectionRun, result["planned_run_id"])
    run = db.get(SuiteRun, planned.suite_run_id)
    assert run.evidence_state == "export_failed"
    assert len(run.observations) == run.total_expected_observations
    categories = [n.category for n in db.query(AutomationNotification).all()]
    assert "manual_collection_export_failed" in categories
    # Sealed evidence cannot be deleted even by direct SQL.
    from sqlalchemy.exc import DBAPIError

    with pytest.raises(DBAPIError):
        with db.bind.connect() as conn:
            conn.execute(text("DELETE FROM suite_runs WHERE id = :id"), {"id": run.id})


def test_24_manual_run_is_labeled_manual_run_now(db, settings, fake_openai, verified_backup):
    plan = _setup_plan(db, settings)
    result = _enqueue(db, settings, plan)
    planned = db.get(PlannedCollectionRun, result["planned_run_id"])
    assert planned.trigger_type == "manual_run_now"
    assert planned.requested_by == "dashboard"
    assert planned.requested_at is not None
    assert planned.idempotency_key == "manual-key-1"


def test_25_cumulative_cost_and_request_counts_include_manual_runs(
    db, settings, fake_openai, verified_backup
):
    plan = _setup_plan(db, settings)
    _enqueue(db, settings, plan)
    from endpoint_ledger.services.scheduler import scheduler_tick

    scheduler_tick(db, settings)
    plan = db.get(CollectionPlan, plan.id)
    assert plan.cumulative_cost_usd > 0
    assert plan_cumulative_request_count(db, plan.id) == 6


def test_26_manual_run_can_advance_baseline_qualification(
    db, fake_openai, verified_backup
):
    small = _small_policy_settings()
    plan = _setup_plan(db, small, repeats=8)
    result = _enqueue(db, small, plan)
    from endpoint_ledger.services.scheduler import scheduler_tick

    scheduler_tick(db, small)
    plan = db.get(CollectionPlan, plan.id)
    assert plan.state == "completed_qualified"
    cohort = db.query(BaselineCohort).filter(
        BaselineCohort.fingerprint == plan.cohort_fingerprint
    ).first()
    assert cohort is not None and cohort.status == "qualified"
    assert db.get(PlannedCollectionRun, result["planned_run_id"]).status == "completed"


def test_27_manual_run_triggers_automatic_qualification_report(
    db, fake_openai, verified_backup
):
    small = _small_policy_settings()
    plan = _setup_plan(db, small, repeats=8)
    _enqueue(db, small, plan)
    from endpoint_ledger.services.scheduler import scheduler_tick

    scheduler_tick(db, small)
    assert db.query(BaselineReport).filter(
        BaselineReport.cohort_fingerprint == plan.cohort_fingerprint
    ).count() == 1
    categories = [n.category for n in db.query(AutomationNotification).all()]
    assert "manual_collection_baseline_qualified" in categories


def test_28_model_reported_change_creates_exactly_one_event(
    db, settings, fake_openai, verified_backup, monkeypatch
):
    plan = _setup_plan(db, settings)
    _set_shifted_model(monkeypatch, fake_openai)
    _enqueue(db, settings, plan)
    from endpoint_ledger.services.scheduler import scheduler_tick

    scheduler_tick(db, settings)
    assert db.query(ModelResolutionEvent).count() == 1
    event = db.query(ModelResolutionEvent).one()
    assert event.requested_model == "gpt-4o-mini"
    assert event.new_model_reported == SHIFTED_MODEL_REPORTED


def _set_shifted_model(monkeypatch, fake):
    import endpoint_ledger.providers as providers
    import endpoint_ledger.runner.service as runner_mod

    def shifted_invoke(endpoint_config, probe_version, repeat_index):
        response = fake.invoke(endpoint_config, probe_version, repeat_index)
        response.model_reported = SHIFTED_MODEL_REPORTED
        return response

    adapter = _AdapterWith(fake, shifted_invoke)
    monkeypatch.setattr(providers, "get_provider", lambda name, settings=None, session=None: adapter)
    monkeypatch.setattr(runner_mod, "get_provider", lambda name, settings=None, session=None: adapter)


def test_29_model_reported_change_pauses_automatic_collection(
    db, settings, fake_openai, verified_backup, monkeypatch
):
    plan = _setup_plan(db, settings, state="enabled")
    _set_shifted_model(monkeypatch, fake_openai)
    _enqueue(db, settings, plan)
    from endpoint_ledger.services.scheduler import scheduler_tick

    scheduler_tick(db, settings)
    plan = db.get(CollectionPlan, plan.id)
    assert plan.state == "paused"
    assert db.query(ModelResolutionEvent).count() == 1


def test_30_no_live_provider_call_occurs(db, settings, fake_openai, verified_backup):
    plan = _setup_plan(db, settings)
    smoke_invocations = fake_openai.invoke_count  # the qualification smoke run
    _enqueue(db, settings, plan)
    from endpoint_ledger.services.scheduler import scheduler_tick

    scheduler_tick(db, settings)
    # The fake adapter answered exactly the expected matrix (6 probes x 1 repeat).
    assert fake_openai.invoke_count == smoke_invocations + 6


def test_31_dashboard_status_pages_and_filters_show_manual_runs(
    db, settings, fake_openai, verified_backup
):
    plan = _setup_plan(db, settings)
    result = _enqueue(db, settings, plan)
    from endpoint_ledger.services.scheduler import scheduler_tick

    scheduler_tick(db, settings)
    planned = db.get(PlannedCollectionRun, result["planned_run_id"])
    client = _app()
    # Status page.
    status = client.get(f"/baseline-ops/manual-run/{planned.id}")
    assert status.status_code == 200
    assert "MANUAL" in status.text
    assert planned.status in status.text
    # Plan detail shows the MANUAL badge.
    detail = client.get(f"/baseline-ops/plan/{plan.id}").text
    assert "MANUAL" in detail
    # Run detail shows the MANUAL badge.
    run_page = client.get(f"/runs/{planned.suite_run_id}").text
    assert "MANUAL" in run_page
    # Notifications page shows the manual badge and categories.
    notifications_page = client.get("/notifications").text
    assert "MANUAL" in notifications_page


def test_32_notifications_generated_once_not_duplicated(
    db, settings, fake_openai, verified_backup
):
    plan = _setup_plan(db, settings)
    _enqueue(db, settings, plan, key="k-once")
    from endpoint_ledger.services.scheduler import scheduler_tick

    scheduler_tick(db, settings)
    categories = [
        n.category for n in db.query(AutomationNotification).order_by(AutomationNotification.id).all()
        if n.category.startswith("manual_collection")
    ]
    assert categories.count("manual_collection_requested") == 1
    assert categories.count("manual_collection_started") == 1
    assert categories.count("manual_collection_completed") == 1
    # Resubmitting the same idempotent request adds no notifications.
    duplicate = request_manual_run(db, settings, plan.id, "k-once")
    assert duplicate.get("duplicate") is True
    again = [
        n.category for n in db.query(AutomationNotification).all()
        if n.category.startswith("manual_collection")
    ]
    assert again.count("manual_collection_requested") == 1
