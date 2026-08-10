"""Dashboard route tests against real database contents."""

from __future__ import annotations

from fastapi.testclient import TestClient

from endpoint_ledger.config import Settings
from endpoint_ledger.runner.service import SuiteOptions, SuiteRunner
from endpoint_ledger.web.app import create_app
from tests.conftest import TEST_DATABASE_URL
from tests.helpers import make_endpoint, sync_probes


def _seed_data(db, settings, mode="stable_success", suite_name="smoke", repeats=5):
    make_endpoint(db, request_defaults={"mock": {"mode": mode, "seed": 1}})
    sync_probes(db)
    db.commit()
    summary = SuiteRunner(db, settings).run(
        SuiteOptions(suite_name=suite_name, provider="mock", repeats=repeats, live=False)
    )
    db.commit()
    return summary.suite_run


def _client(engine, settings):
    settings = Settings(database_url=settings.database_url, show_raw_responses=settings.show_raw_responses)
    return TestClient(create_app(settings))


def test_overview_shows_real_data(db, settings):
    run = _seed_data(db, settings)
    client = _client(db, settings)
    # Mock runs are origin=mock; the dashboard defaults to live, so the data
    # shows under the explicit mock origin filter.
    resp = client.get("/?origin=mock")
    assert resp.status_code == 200
    body = resp.text
    assert "Endpoint Ledger" in body
    assert "Observations (mock)" in body
    assert f"/runs/{run.id}" in body


def test_live_default_excludes_mock_data(db, settings):
    _seed_data(db, settings)
    client = _client(db, settings)
    body = client.get("/").text  # default origin = live
    assert "Observations (live)" in body
    assert "No eligible primary observations" in body


def test_all_detail_routes_render(db, settings):
    run = _seed_data(db, settings)
    client = _client(db, settings)
    from endpoint_ledger.db.models import Observation, Probe, ProviderEndpoint

    endpoint = db.query(ProviderEndpoint).first()
    observation = db.query(Observation).first()
    probe = db.query(Probe).first()

    assert client.get(f"/endpoints/{endpoint.id}?origin=mock").status_code == 200
    assert client.get(f"/runs/{run.id}").status_code == 200
    assert client.get(f"/probes/{probe.id}?origin=mock").status_code == 200
    assert client.get(f"/observations/{observation.id}").status_code == 200
    assert client.get("/changes").status_code == 200


def test_observation_detail_exposes_probe_version_and_scores(db, settings):
    _seed_data(db, settings)
    client = _client(db, settings)
    from endpoint_ledger.db.models import Observation

    observation = db.query(Observation).first()
    body = client.get(f"/observations/{observation.id}").text
    assert "Exact probe version" in body
    assert observation.probe_version.definition_hash in body
    assert "json_parse_success" in body  # scorer evidence present
    assert observation.raw_request_hash in body
    assert "Primary outcome" in body
    assert observation.suite_run.data_origin in body  # origin badge


def test_raw_response_hidden_by_default(db, settings):
    _seed_data(db, settings)
    client = _client(db, settings)
    from endpoint_ledger.db.models import Observation

    observation = db.query(Observation).filter(Observation.raw_response.is_not(None)).first()
    assert observation is not None
    body = client.get(f"/observations/{observation.id}").text
    assert "Raw response hidden" in body


def test_raw_response_shown_when_enabled(db):
    settings = Settings(database_url=TEST_DATABASE_URL, show_raw_responses=True)
    _seed_data(db, settings)
    client = _client(db, settings)
    from endpoint_ledger.db.models import Observation

    observation = db.query(Observation).filter(Observation.raw_response.is_not(None)).first()
    body = client.get(f"/observations/{observation.id}").text
    assert "Raw response hidden" not in body
    assert '"provider": "mock"' in body


def test_changes_page_excludes_demo_by_default(db, settings):
    from endpoint_ledger.change_detection.service import detect_changes
    from endpoint_ledger.db.models import ProviderEndpoint, SuiteRun

    # Demo history: baseline + regression, origin=demo.
    _seed_data(db, settings, mode="stable_success", suite_name="demo-baseline")
    demo_endpoint = db.query(ProviderEndpoint).filter(ProviderEndpoint.provider == "mock").first()
    demo_endpoint.request_defaults = {"mock": {"mode": "structured_output_regression", "seed": 1}}
    db.commit()

    baseline_run = (
        db.query(SuiteRun).filter(SuiteRun.suite_name == "demo-baseline").first()
    )
    boundary = max(o.completed_at or o.requested_at for o in baseline_run.observations)
    _seed_data(db, settings, mode="structured_output_regression", suite_name="demo-regression")

    result = detect_changes(
        db, settings, split_after=boundary,
        origin="demo",  # type: ignore[arg-type]
        require_qualified_cohort=False,
    )
    assert result.events, "demo regression should produce demo change events"

    client = _client(db, settings)
    # Default live view hides demo events.
    live_body = client.get("/changes").text
    assert "(origin: live)" in live_body
    assert "DEMO" not in live_body
    # Explicit all-origin view shows them with the DEMO badge.
    all_body = client.get("/changes?origin=all").text
    assert "DEMO" in all_body


def test_missing_routes_return_404(db, settings):
    client = _client(db, settings)
    assert client.get("/runs/999999").status_code == 404
    assert client.get("/endpoints/999999").status_code == 404
    assert client.get("/observations/999999").status_code == 404
