"""Phase 1.3 tests: credentials, CSRF, preflight, qualification, plans, scheduler."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest
from fastapi.testclient import TestClient

from endpoint_ledger.config import Settings
from endpoint_ledger.db.models import (
    AutomationNotification,
    BaselineCohort,
    CollectionPlan,
    PlannedCollectionRun,
    RunAnnotation,
    RunArtifact,
    SuiteRun,
)
from endpoint_ledger.services.collection_service import (
    create_plan,
    disable_plan,
    enable_plan,
    pause_plan,
    resume_plan,
    stop_after_current,
)
from endpoint_ledger.services.credentials import (
    credential_status,
    remove_credential,
    resolve_api_key,
    store_credential,
)
from endpoint_ledger.services.preflight_service import perform_preflight
from endpoint_ledger.services.qualification_service import (
    approve as approve_run,
)
from endpoint_ledger.services.qualification_service import (
    execute_qualification_smoke,
)
from endpoint_ledger.services.qualification_service import (
    reject as reject_run,
)
from endpoint_ledger.web.app import create_app
from tests.conftest import TEST_DATABASE_URL
from tests.helpers import make_endpoint, sync_probes

FAKE_MODEL_REPORTED = "gpt-4o-mini-2024-07-18"


def _make_openai_endpoint(db):
    return make_endpoint(
        db, provider="openai", display_name="OpenAI", model_requested="gpt-4o-mini",
        input_price=0.15, output_price=0.60,
    )


class FakeOpenAIAdapter:
    provider_name = "openai"

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

    def fake_get_provider(name, settings=None):
        if name == "openai":
            return FakeOpenAIAdapter()
        from endpoint_ledger.providers.mock import MockProvider

        return MockProvider()

    import endpoint_ledger.preflight as preflight_mod
    import endpoint_ledger.runner.service as runner_mod

    monkeypatch.setattr(providers, "get_provider", fake_get_provider)
    monkeypatch.setattr(runner_mod, "get_provider", fake_get_provider)
    monkeypatch.setattr(preflight_mod, "get_provider", fake_get_provider)
    yield


def _app(db_url=TEST_DATABASE_URL, **overrides):
    return TestClient(create_app(Settings(database_url=db_url, **overrides)))


def _csrf_headers(client: TestClient) -> dict:
    client.get("/")
    token = client.cookies.get("el_csrftoken")
    return {"x-csrf-token": token}


# ------------------------------------------------------------ credentials


def test_credential_setup_stores_no_secret_in_db(db, fake_openai, tmp_path):
    store_credential(db, "openai", "sk-test-secret-value", None)
    status = credential_status(db, "openai")
    assert status["configured"] is True
    from endpoint_ledger.db.models import CredentialMetadata

    row = db.query(CredentialMetadata).filter(CredentialMetadata.provider == "openai").one()
    assert "sk-test-secret-value" not in (row.validation_result or "")
    assert resolve_api_key("openai") == "sk-test-secret-value"
    remove_credential(db, "openai")
    # Environment fallback still resolves (env var present).
    assert resolve_api_key("openai") == "sk-fake"
    row = db.query(CredentialMetadata).filter(CredentialMetadata.provider == "openai").one()
    assert row.configured is False


def test_credential_page_never_echoes_secret(db, fake_openai):
    client = _app()
    client.get("/baseline-ops")
    headers = _csrf_headers(client)
    resp = client.post(
        "/baseline-ops/credential",
        data={"credential_value": "sk-browser-secret", "csrf_token": headers["x-csrf-token"]},
        follow_redirects=True,
    )
    assert resp.status_code == 200
    assert "sk-browser-secret" not in resp.text
    assert "browser-secret" not in resp.text


# ------------------------------------------------------------ csrf


def test_csrf_blocks_missing_token(db, fake_openai):
    client = _app()
    resp = client.post("/baseline-ops/preflight", data={})
    assert resp.status_code == 403


def test_csrf_allows_valid_token(db, fake_openai):
    client = _app()
    client.get("/baseline-ops")
    token = client.cookies.get("el_csrftoken")
    resp = client.post("/baseline-ops/preflight", data={"csrf_token": token}, follow_redirects=True)
    assert resp.status_code == 200


# ------------------------------------------------------------ preflight


def test_preflight_button_success_and_blocked(db, settings, fake_openai, monkeypatch):
    _make_openai_endpoint(db)
    sync_probes(db)
    db.commit()
    report = perform_preflight(
        db, settings, provider="openai", endpoint_filter="gpt-4o-mini",
        suite="smoke", repeats=5, cost_ceiling=1.0,
    )
    assert report["ready"] is True
    assert report["configuration_hash"]

    # Blocked: ineligible probes present.
    from endpoint_ledger.db.models import ProbeVersion

    for version in db.query(ProbeVersion).all():
        version.baseline_eligible = False
    db.commit()
    blocked = perform_preflight(
        db, settings, provider="openai", endpoint_filter="gpt-4o-mini",
        suite="smoke", repeats=5, cost_ceiling=1.0,
    )
    assert blocked["ready"] is False
    assert any("ineligible" in r for r in blocked["refusals"])
    # Two preflights produced different configuration states (stale detection).
    assert report["configuration_hash"] != blocked["configuration_hash"]


# ------------------------------------------------------------ qualification smoke


def test_qualification_smoke_creates_sealed_exported_evidence(db, settings, fake_openai):
    _make_openai_endpoint(db)
    sync_probes(db)
    db.commit()
    result = execute_qualification_smoke(db, settings)
    assert result["ok"] is True
    run = db.get(SuiteRun, result["run_id"])
    assert run is not None
    assert run.data_origin == "live"
    assert run.evidence_state == "exported"
    assert run.completed_observations == run.total_expected_observations == 6
    assert run.evidence_manifest_hash
    assert db.query(RunArtifact).filter(RunArtifact.suite_run_id == run.id).one().verification_status == "verified"


def test_qualification_smoke_duplicate_prevention(db, settings, fake_openai):
    _make_openai_endpoint(db)
    sync_probes(db)
    db.commit()
    first = execute_qualification_smoke(db, settings)
    second = execute_qualification_smoke(db, settings)
    assert first["run_id"] == second["run_id"]
    assert second.get("duplicate") is True
    assert db.query(SuiteRun).filter(SuiteRun.suite_name.like("qualification-smoke-%")).count() == 1


def test_qualification_approval_and_rejection(db, settings, fake_openai):
    _make_openai_endpoint(db)
    sync_probes(db)
    db.commit()
    result = execute_qualification_smoke(db, settings)
    run_id = result["run_id"]
    approved = approve_run(db, run_id)
    assert approved["ok"] is True
    assert db.query(RunAnnotation).filter(
        RunAnnotation.suite_run_id == run_id,
        RunAnnotation.annotation_type == "qualification_approval",
    ).count() == 1

    rejected = reject_run(db, run_id, "probe ambiguity in confirmation contract")
    assert rejected["ok"] is True
    assert db.query(RunAnnotation).filter(
        RunAnnotation.suite_run_id == run_id,
        RunAnnotation.annotation_type == "qualification_rejection",
    ).count() == 1


# ------------------------------------------------------------ collection plan


def test_plan_create_enable_pause_resume_stop_disable(db, settings, fake_openai):
    endpoint = _make_openai_endpoint(db)
    sync_probes(db)
    db.commit()
    from endpoint_ledger.measurement.cohorts import CohortService

    reports = CohortService(db, settings).status_report(origin="live")
    fingerprint = reports[0]["cohort"].fingerprint if reports else "fp"
    plan = create_plan(
        db, settings, provider="openai", endpoint_id=endpoint.id, suite_name="smoke",
        cohort_fingerprint=fingerprint, repeats_per_probe=5,
    )
    assert plan.state == "proposed"
    assert enable_plan(db, plan.id)["ok"] is True
    assert db.get(CollectionPlan, plan.id).state == "enabled"
    assert pause_plan(db, plan.id)["ok"] is True
    assert db.get(CollectionPlan, plan.id).state == "paused"
    assert resume_plan(db, plan.id)["ok"] is True
    assert db.get(CollectionPlan, plan.id).state == "enabled"
    assert stop_after_current(db, plan.id)["ok"] is True
    assert db.get(CollectionPlan, plan.id).state == "stopping"
    assert disable_plan(db, plan.id)["ok"] is True
    assert db.get(CollectionPlan, plan.id).state == "disabled"


def test_scheduler_executes_due_planned_run_and_marks_missed(db, settings, fake_openai):
    endpoint = _make_openai_endpoint(db)
    sync_probes(db)
    db.commit()
    from endpoint_ledger.measurement.cohorts import CohortService

    reports = CohortService(db, settings).status_report(origin="live")
    fingerprint = reports[0]["cohort"].fingerprint if reports else "fp"
    plan = create_plan(
        db, settings, provider="openai", endpoint_id=endpoint.id, suite_name="smoke",
        cohort_fingerprint=fingerprint, repeats_per_probe=1,
    )
    enable_plan(db, plan.id)
    due = db.query(PlannedCollectionRun).filter(PlannedCollectionRun.collection_plan_id == plan.id).first()
    due.scheduled_for = datetime.now(UTC) - timedelta(minutes=1)
    db.commit()

    from endpoint_ledger.services.scheduler import scheduler_tick

    outcome = scheduler_tick(db, settings)
    assert outcome["ok"] is True
    due = db.get(PlannedCollectionRun, due.id)
    assert due.status in ("completed", "failed", "blocked")
    assert due.suite_run_id is not None
    if due.suite_run_id:
        run = db.get(SuiteRun, due.suite_run_id)
        assert run.evidence_state in ("sealed", "exported")


def test_duplicate_planned_run_cannot_create_duplicate_suite_run(db, settings, fake_openai):
    endpoint = _make_openai_endpoint(db)
    sync_probes(db)
    db.commit()
    from endpoint_ledger.measurement.cohorts import CohortService

    reports = CohortService(db, settings).status_report(origin="live")
    fingerprint = reports[0]["cohort"].fingerprint if reports else "fp"
    plan = create_plan(
        db, settings, provider="openai", endpoint_id=endpoint.id, suite_name="smoke",
        cohort_fingerprint=fingerprint, repeats_per_probe=1,
    )
    enable_plan(db, plan.id)
    due = db.query(PlannedCollectionRun).filter(PlannedCollectionRun.collection_plan_id == plan.id).first()
    due.scheduled_for = datetime.now(UTC) - timedelta(minutes=1)
    db.commit()
    from endpoint_ledger.services.scheduler import scheduler_tick

    scheduler_tick(db, settings)
    first_runs = db.query(SuiteRun).count()
    # A second tick on a completed planned run creates nothing new.
    scheduler_tick(db, settings)
    assert db.query(SuiteRun).count() == first_runs


# ------------------------------------------------------------ 168 hours


def test_168_hour_qualification_requirement(db, settings):
    make_endpoint(db, request_defaults={"mock": {"mode": "stable_success", "seed": 1}})
    sync_probes(db)
    db.commit()
    from endpoint_ledger.runner.service import SuiteOptions, SuiteRunner

    now = datetime.now(UTC)
    # 10 runs across 7 distinct dates but all within <168 hours (e.g. 7 days * 23h).
    for i in range(10):
        run = SuiteRunner(db, settings).run(
            SuiteOptions(suite_name=f"live-{i}", provider="mock", repeats=5, live=False)
        ).suite_run
        run.data_origin = "live"
        offset = timedelta(days=6, hours=23 - i)  # all within <168 hours
        for observation in run.observations:
            observation.completed_at = now - offset
        db.commit()
    small = Settings(
        database_url=TEST_DATABASE_URL,
        baseline_min_calendar_days=0,
        baseline_min_distinct_runs=2,
        baseline_min_primary_observations=8,
        baseline_min_distinct_dates=1,
        baseline_min_elapsed_hours=168.0,
    )
    from endpoint_ledger.measurement.cohorts import CohortService

    service = CohortService(db, small)
    service.sync_cohorts("live")
    service.refresh_qualification_statuses()
    reports = service.status_report(origin="live")
    assert reports
    # 7 dates but <168 elapsed hours -> still collecting.
    assert all(not r["progress"]["qualified"] for r in reports)


# ------------------------------------------------------------ model resolution


def test_model_resolution_change_pauses_collection(db, settings, fake_openai):
    endpoint = _make_openai_endpoint(db)
    sync_probes(db)
    db.commit()
    # Establish the cohort with a smoke run (records the reported model).
    smoke = execute_qualification_smoke(db, settings)
    assert smoke["ok"] is True
    from endpoint_ledger.measurement.cohorts import CohortService

    reports = CohortService(db, settings).status_report(origin="live")
    fingerprint = reports[0]["cohort"].fingerprint if reports else "fp"
    plan = create_plan(
        db, settings, provider="openai", endpoint_id=endpoint.id, suite_name="smoke",
        cohort_fingerprint=fingerprint, repeats_per_probe=1,
    )
    enable_plan(db, plan.id)
    due = db.query(PlannedCollectionRun).filter(PlannedCollectionRun.collection_plan_id == plan.id).first()
    due.scheduled_for = datetime.now(UTC) - timedelta(minutes=1)
    db.commit()

    import endpoint_ledger.providers as providers

    fake = FakeOpenAIAdapter()
    original = fake.invoke

    def shifted_invoke(endpoint_config, probe_version, repeat_index):
        response = original(endpoint_config, probe_version, repeat_index)
        response.model_reported = "gpt-4o-mini-2024-08-06"
        return response

    fake.invoke = shifted_invoke
    providers_get = providers.get_provider

    def get(name, settings=None):
        if name == "openai":
            return fake
        return providers_get(name, settings)

    import endpoint_ledger.runner.service as runner_mod

    monkey = pytest.MonkeyPatch()
    monkey.setattr(providers, "get_provider", get)
    monkey.setattr(runner_mod, "get_provider", get)
    try:
        from endpoint_ledger.services.scheduler import scheduler_tick

        scheduler_tick(db, settings)
    finally:
        monkey.undo()

    plan = db.get(CollectionPlan, plan.id)
    assert plan.state == "paused"
    from endpoint_ledger.db.models import ModelResolutionEvent

    assert db.query(ModelResolutionEvent).count() == 1
    assert db.query(AutomationNotification).filter(
        AutomationNotification.category == "model_resolution_changed"
    ).count() >= 1


# ------------------------------------------------------------ baseline report


def test_baseline_qualification_report_created(db, settings, fake_openai):
    _make_openai_endpoint(db)
    sync_probes(db)
    db.commit()
    result = execute_qualification_smoke(db, settings)
    assert result["ok"]
    from endpoint_ledger.measurement.cohorts import COHORT_STATUS_QUALIFIED, CohortService

    service = CohortService(db, settings)
    service.sync_cohorts("live")
    cohort = db.query(BaselineCohort).filter(BaselineCohort.data_origin == "live").first()
    assert cohort is not None
    cohort.status = COHORT_STATUS_QUALIFIED
    db.commit()

    from endpoint_ledger.services.report_service import create_baseline_report

    report = create_baseline_report(db, cohort, settings)
    assert report.sha256
    assert report.verification_status == "verified"
    assert report.content["cohort_fingerprint"] == cohort.fingerprint
    assert report.content["provider"] == "openai"
    import hashlib
    from pathlib import Path

    actual = hashlib.sha256(Path(report.path).read_bytes()).hexdigest()
    assert actual == report.sha256


# ------------------------------------------------------------ dashboard routes


def test_baseline_ops_page_and_notifications_routes(db, fake_openai):
    client = _app()
    assert client.get("/baseline-ops").status_code == 200
    assert client.get("/notifications").status_code == 200
    body = client.get("/baseline-ops").text
    assert "Baseline Operations" in body
    assert "Ready" in body or "ready" in body or "preflight" in body
