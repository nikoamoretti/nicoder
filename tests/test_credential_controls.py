"""Credential-control tests: secure OpenAI credential workflow (single authority).

Covers the always-visible credential section across baseline-ops states, the
POST-only + CSRF-protected keychain entry, guarantee that the secret never
reaches PostgreSQL/HTTP responses/logs, explicit active-source selection, and
preflight binding invalidation — without any live OpenAI request.
"""

from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from endpoint_ledger import services
from endpoint_ledger.config import Settings
from endpoint_ledger.db.models import (
    AutomationNotification,
    CredentialMetadata,
    Observation,
    PreflightRecord,
    RunAnnotation,
    Score,
    SuiteRun,
)
from endpoint_ledger.runner.service import SuiteOptions, SuiteRunner
from endpoint_ledger.services.credentials import (
    INVALIDATION_REASON,
    CredentialResolutionError,
    _account,
    active_source,
    credential_status,
    fingerprint_of,
    remove_credential,
    resolve_credential,
    set_active_source,
    source_value,
    store_credential,
    validate_and_store_credential,
    validate_credential,
)
from endpoint_ledger.services.preflight_service import latest_preflight, perform_preflight
from endpoint_ledger.services.qualification_service import execute_qualification_smoke
from endpoint_ledger.web.app import create_app
from tests.conftest import TEST_DATABASE_URL
from tests.helpers import make_endpoint, sync_probes
from tests.test_baseline_operations import FakeOpenAIAdapter

SECRET = "sk-controls-secret-42"


@pytest.fixture(autouse=True)
def _controlled_env(monkeypatch):
    """Never resolve the developer's real OPENAI_API_KEY in tests."""
    monkeypatch.setenv("OPENAI_API_KEY", "sk-controlled-test")

IDENTITY_PAYLOAD = {
    "id": "user-abc-123",
    "email": "engineer@example.com",
    "orgs": [{"id": "org-1", "title": "Platform"}],
}



def seed_keychain(value=SECRET, settings=None):
    """Populate the keyring directly, leaving no explicit active-source selection."""
    import keyring

    from endpoint_ledger.config import get_settings

    keyring.set_password(_account("openai", settings or get_settings()), "openai", value)


def _client(db_url: str = TEST_DATABASE_URL, **overrides) -> TestClient:
    return TestClient(create_app(Settings(database_url=db_url, **overrides)))


def _csrf(client: TestClient) -> str:
    client.get("/baseline-ops")
    return client.cookies.get("el_csrftoken", "")


def _store_via_route(client: TestClient, value: str = SECRET, *, token: str | None = None) -> None:
    token = token if token is not None else _csrf(client)
    response = client.post(
        "/baseline-ops/credential",
        data={"credential_value": value, "csrf_token": token},
        follow_redirects=False,
    )
    assert response.status_code == 303


def _fake_identity(monkeypatch, payload=IDENTITY_PAYLOAD, captured: list | None = None):
    def _identity(api_key: str, timeout: float):
        if captured is not None:
            captured.append(api_key)
        if callable(payload):
            raise payload(api_key)
        return payload

    monkeypatch.setattr(
        "endpoint_ledger.services.credentials._identity_request", _identity
    )


def _openai_endpoint_and_probes(db, settings) -> None:
    make_endpoint(
        db, provider="openai", display_name="OpenAI", model_requested="gpt-4o-mini",
        input_price=0.15, output_price=0.60,
    )
    sync_probes(db)
    db.commit()


def _bound_preflight(db, settings, monkeypatch):
    """Environment credential + ready preflight bound to it. Returns fingerprint."""
    monkeypatch.setenv("OPENAI_API_KEY", "sk-fake")
    _openai_endpoint_and_probes(db, settings)
    report = perform_preflight(
        db, settings, provider="openai", endpoint_filter="gpt-4o-mini",
        suite="smoke", repeats=1, cost_ceiling=1.0,
    )
    assert report["ready"] is True, report["refusals"]
    record = latest_preflight(db, "openai")
    assert record is not None
    assert record.binding_valid is True
    return record.credential_fingerprint


def _fake_providers(monkeypatch):
    import endpoint_ledger.providers as providers
    import endpoint_ledger.runner.service as runner_mod

    def fake_get_provider(name, settings=None, session=None):
        if name == "openai":
            return FakeOpenAIAdapter()
        from endpoint_ledger.providers.mock import MockProvider

        return MockProvider()

    monkeypatch.setattr(providers, "get_provider", fake_get_provider)
    monkeypatch.setattr(runner_mod, "get_provider", fake_get_provider)


class TestControlsRenderedInEveryState:
    def test_credential_required_state_renders_controls(self, db, settings, monkeypatch):
        monkeypatch.delenv("OPENAI_API_KEY", raising=False)
        client = _client()
        body = client.get("/baseline-ops").text
        assert "credential_required" in body
        assert "OpenAI API Key" in body
        assert 'type="password"' in body
        assert "OpenAI API key" in body
        assert "Save and validate" in body
        assert "Stored securely on this Mac. The key will not be displayed again." in body
        assert "keychain" not in body.lower()
        assert "environment" not in body.lower()
        assert "fingerprint" not in body.lower()

    def test_external_credential_still_offers_the_simple_entry_form(self, db, settings, monkeypatch):
        monkeypatch.setenv("OPENAI_API_KEY", "sk-fake")
        body = _client().get("/baseline-ops").text
        assert "An externally configured key exists." in body
        assert "OpenAI API Key" in body
        assert "Save and validate" in body

    def test_ready_for_qualification_smoke_renders_controls(self, db, settings, monkeypatch):
        _bound_preflight(db, settings, monkeypatch)
        body = _client().get("/baseline-ops").text
        assert "ready_for_qualification_smoke" in body
        assert "OpenAI API Key" in body
        assert "Save and validate" in body

    def test_ready_for_review_renders_controls(self, db, settings, monkeypatch):
        _fake_providers(monkeypatch)
        _bound_preflight(db, settings, monkeypatch)
        result = execute_qualification_smoke(db, settings)
        assert result["ok"] is True
        body = _client().get("/baseline-ops").text
        assert "qualification_smoke_ready_for_review" in body
        assert "OpenAI API Key" in body
        assert "Save and validate" in body
        assert "Review qualification run" in body

    def test_rejected_state_renders_controls(self, db, settings, monkeypatch):
        from endpoint_ledger.services.qualification_service import reject

        _fake_providers(monkeypatch)
        _bound_preflight(db, settings, monkeypatch)
        result = execute_qualification_smoke(db, settings)
        response = reject(db, result["run_id"], "operational failure: account configuration")
        assert response["ok"] is True
        body = _client().get("/baseline-ops").text
        assert "qualification_smoke_rejected" in body
        assert "OpenAI API Key" in body
        assert "Save and validate" in body


class TestEntryIsPostOnlyAndCsrfProtected:
    def test_rendered_credential_form_uses_the_csrf_cookie(self, db, settings):
        client = _client()
        response = client.get("/baseline-ops")
        token = client.cookies.get("el_csrftoken")
        assert token
        assert f'name="csrf_token" value="{token}"' in response.text

    def test_get_on_credential_route_is_rejected(self, db, settings):
        client = _client()
        client.get("/baseline-ops")
        assert client.get("/baseline-ops/credential").status_code == 405
        assert client.get("/baseline-ops/credential/remove").status_code == 405

    def test_post_without_csrf_token_is_forbidden(self, db, settings):
        client = _client()
        client.get("/baseline-ops")
        response = client.post(
            "/baseline-ops/credential", data={"credential_value": SECRET}
        )
        assert response.status_code == 403

    def test_post_with_foreign_csrf_token_is_forbidden(self, db, settings):
        client = _client()
        client.get("/baseline-ops")
        response = client.post(
            "/baseline-ops/credential",
            data={"credential_value": SECRET, "csrf_token": "attacker-token"},
        )
        assert response.status_code == 403

    def test_remove_requires_confirmation(self, db, settings, monkeypatch):
        store_credential(db, "openai", SECRET, settings)
        client = _client()
        token = _csrf(client)
        response = client.post(
            "/baseline-ops/credential/remove", data={"csrf_token": token, "confirm": "wrong"}
        )
        assert response.status_code == 400
        assert source_value("openai", "keychain", settings) == SECRET


class TestSecretNeverLeavesKeychain:
    def test_store_writes_keychain_defaults_to_keychain(self, db, settings, monkeypatch):
        _fake_identity(monkeypatch)
        monkeypatch.delenv("OPENAI_API_KEY", raising=False)
        status = store_credential(db, "openai", SECRET, settings)
        assert source_value("openai", "keychain", settings) == SECRET
        assert source_value("openai", "environment", settings) is None
        assert status["active_source"] == "keychain"
        assert status["explicit_source"] == "keychain"
        assert status["keychain"]["fingerprint"] == fingerprint_of(SECRET)
        assert active_source(db, "openai", settings) == "keychain"

    def test_secret_never_in_database(self, db, settings, monkeypatch):
        _fake_identity(monkeypatch)
        store_credential(db, "openai", SECRET, settings)
        _openai_endpoint_and_probes(db, settings)
        _bound_preflight(db, settings, monkeypatch)
        db.flush()
        rows: list[str] = []
        for table in (SuiteRun, Observation, Score, CredentialMetadata, PreflightRecord):
            for row in db.query(table).all():
                rows.append(str(row.__dict__.values()))
        assert SECRET not in "".join(rows)
        assert SECRET not in str(
            db.query(CredentialMetadata).one_or_none().__dict__
        )

    def test_secret_never_in_http_response_or_redirect(self, db, settings, monkeypatch):
        _fake_identity(monkeypatch)
        client = _client()
        token = _csrf(client)
        response = client.post(
            "/baseline-ops/credential",
            data={"credential_value": SECRET, "csrf_token": token},
            follow_redirects=True,
        )
        assert SECRET not in response.text
        body = client.get("/baseline-ops").text
        assert SECRET not in body
        assert fingerprint_of(SECRET) not in body
        assert "Status:" in body
        assert "Configured" in body

    def test_secret_never_in_logs(self, db, settings, monkeypatch, caplog):
        import logging

        _fake_identity(monkeypatch)
        with caplog.at_level(logging.DEBUG):
            store_credential(db, "openai", SECRET, settings)
            try:
                resolve_credential(db, "openai", settings)
            except CredentialResolutionError:
                pass
        assert SECRET not in caplog.text

    def test_remove_clears_keychain_and_selection_without_fallback(self, db, settings, monkeypatch):
        monkeypatch.setenv("OPENAI_API_KEY", "sk-fake")
        store_credential(db, "openai", SECRET, settings)
        status = remove_credential(db, "openai", settings)
        assert source_value("openai", "keychain", settings) is None
        assert status["active_source"] == "environment"
        assert status["explicit_source"] is None
        assert credential_status(db, "openai", settings)["configured"] is True
        assert resolve_credential(db, "openai", settings).source == "environment"

    def test_route_save_and_validate_records_notification(self, db, settings, monkeypatch):
        _fake_identity(monkeypatch)
        client = _client()
        _store_via_route(client)
        notification = (
            db.query(AutomationNotification)
            .filter(AutomationNotification.category == services.notifications.CATEGORY_CREDENTIAL_CHANGED)
            .one_or_none()
        )
        assert notification is not None
        assert SECRET not in notification.title and SECRET not in notification.message

    def test_invalid_candidate_does_not_replace_existing_valid_key(self, db, settings, monkeypatch):
        _fake_identity(monkeypatch)
        saved = validate_and_store_credential(db, "openai", SECRET, settings)
        assert saved["ok"] is True

        def _invalid(api_key: str, timeout: float):
            raise RuntimeError("invalid API key")

        result = validate_and_store_credential(
            db, "openai", "sk-invalid-candidate", settings, identity_client=_invalid
        )
        assert result["ok"] is False
        assert source_value("openai", "keychain", settings) == SECRET
        assert resolve_credential(db, "openai", settings).value == SECRET


class TestIdentityValidation:
    def test_validation_uses_identity_endpoint_only_with_redaction(self, db, settings, monkeypatch):
        captured: list[str] = []
        _fake_identity(monkeypatch, captured=captured)
        store_credential(db, "openai", SECRET, settings)
        validate_credential(db, "openai", settings)
        metadata = db.query(CredentialMetadata).one()
        assert metadata.validation_status == "valid"
        assert captured == [SECRET]  # identity call, not a model request
        assert metadata.identity_user_id == "user-abc-123"
        assert metadata.identity_email_redacted == "e***@example.com"
        assert metadata.identity_orgs == [{"id": "org-1", "title": "Platform"}]

    def test_validation_failure_is_invalid_and_sanitized(self, db, settings, monkeypatch):
        def _boom(api_key: str, timeout: float):
            raise RuntimeError("Connection refused")

        _fake_identity(monkeypatch, payload=_boom)
        store_credential(db, "openai", SECRET, settings)
        result = validate_credential(db, "openai", settings)
        assert result["ok"] is False
        metadata = db.query(CredentialMetadata).one()
        assert metadata.validation_status == "invalid"
        assert SECRET not in metadata.validation_message

    def test_identity_payload_never_leaks_email_domain_suffix(self, db, settings, monkeypatch):
        _fake_identity(
            monkeypatch,
            payload={"id": "u1", "email": "secret-user@example.com", "orgs": []},
        )
        store_credential(db, "openai", SECRET, settings)
        validate_credential(db, "openai", settings)
        metadata = db.query(CredentialMetadata).one()
        assert metadata.identity_email_redacted == "s***@example.com"
        assert "secret-user" not in metadata.identity_email_redacted


class TestExplicitActiveSourcePolicy:
    def test_ambiguous_refuses_resolution(self, db, settings, monkeypatch):
        monkeypatch.setenv("OPENAI_API_KEY", "sk-fake-env")
        seed_keychain(SECRET, settings)
        status = credential_status(db, "openai", settings)
        assert status["ambiguous"] is True
        with pytest.raises(CredentialResolutionError) as excinfo:
            resolve_credential(db, "openai", settings)
        assert excinfo.value.reason == "ambiguous"

    def test_explicit_selection_resolves_without_ambiguity(self, db, settings, monkeypatch):
        monkeypatch.setenv("OPENAI_API_KEY", "sk-fake-env")
        store_credential(db, "openai", SECRET, settings)
        set_active_source(db, "openai", "keychain", settings)
        assert resolve_credential(db, "openai", settings).source == "keychain"
        set_active_source(db, "openai", "environment", settings)
        resolved = resolve_credential(db, "openai", settings)
        assert resolved.source == "environment"
        assert resolved.fingerprint == fingerprint_of("sk-fake-env")

    def test_dashboard_key_is_authoritative_over_environment(self, db, settings, monkeypatch):
        monkeypatch.setenv("OPENAI_API_KEY", "sk-external")
        _fake_identity(monkeypatch)
        result = validate_and_store_credential(db, "openai", SECRET, settings)
        assert result["ok"] is True
        resolved = resolve_credential(db, "openai", settings)
        assert resolved.source == "keychain"
        assert resolved.value == SECRET

    def test_selecting_missing_source_refuses(self, db, settings, monkeypatch):
        monkeypatch.setenv("OPENAI_API_KEY", "sk-fake-env")
        with pytest.raises(CredentialResolutionError):
            set_active_source(db, "openai", "keychain", settings)

    def test_no_credential_at_all_is_no_credential(self, db, settings, monkeypatch):
        monkeypatch.delenv("OPENAI_API_KEY", raising=False)
        with pytest.raises(CredentialResolutionError) as excinfo:
            resolve_credential(db, "openai", settings)
        assert excinfo.value.reason == "no_credential"


class TestPreflightBinding:
    def test_preflight_binds_active_credential_fingerprint(self, db, settings, monkeypatch):
        fingerprint = _bound_preflight(db, settings, monkeypatch)
        credential = resolve_credential(db, "openai", settings)
        assert fingerprint == credential.fingerprint
        record = latest_preflight(db, "openai")
        assert record.source_binding == "environment"
        status = credential_status(db, "openai", settings)
        assert status["preflight_binding"]["ok"] is True

    def test_store_after_preflight_invalidates_binding(self, db, settings, monkeypatch):
        _bound_preflight(db, settings, monkeypatch)
        store_credential(db, "openai", SECRET, settings)
        record = latest_preflight(db, "openai")
        assert record.binding_valid is False
        assert record.binding_reason == INVALIDATION_REASON
        status = credential_status(db, "openai", settings)
        assert status["preflight_binding"]["ok"] is False
        assert status["preflight_binding"]["reason"] == INVALIDATION_REASON

    def test_source_switch_invalidates_binding(self, db, settings, monkeypatch):
        monkeypatch.setenv("OPENAI_API_KEY", "sk-fake")
        store_credential(db, "openai", SECRET, settings)
        _openai_endpoint_and_probes(db, settings)
        perform_preflight(
            db, settings, provider="openai", endpoint_filter="gpt-4o-mini",
            suite="smoke", repeats=1, cost_ceiling=1.0,
        )
        record = latest_preflight(db, "openai")
        assert record.source_binding == "keychain"
        assert record.binding_valid is True
        set_active_source(db, "openai", "environment", settings)
        record = latest_preflight(db, "openai")
        assert record.binding_valid is False
        assert record.binding_reason == INVALIDATION_REASON

    def test_route_saving_key_invalidates_preflight(self, db, settings, monkeypatch):
        _bound_preflight(db, settings, monkeypatch)
        _fake_identity(monkeypatch)
        client = _client()
        token = _csrf(client)
        response = client.post(
            "/baseline-ops/credential",
            data={"credential_value": SECRET, "csrf_token": token},
            follow_redirects=False,
        )
        assert response.status_code == 303
        record = latest_preflight(db, "openai")
        assert record.binding_valid is False


class TestLiveRunGuard:
    def _run(self, db, settings, monkeypatch):
        _fake_providers(monkeypatch)
        _openai_endpoint_and_probes(db, settings)
        options = SuiteOptions(
            suite_name="smoke", provider="openai", endpoint_filter="gpt-4o-mini",
            repeats=1, live=True, cost_ceiling_usd=1.0, eligible_only=True,
        )
        return SuiteRunner(db, settings).run(options)

    def test_missing_preflight_blocks_live_run(self, db, settings, monkeypatch):
        monkeypatch.setenv("OPENAI_API_KEY", "sk-fake")
        summary = self._run(db, settings, monkeypatch)
        assert summary.suite_run is not None
        run = db.get(SuiteRun, summary.suite_run.id)
        assert run.status == "failed"
        assert "PREFLIGHT REQUIRED BEFORE LIVE RUN" in run.error_summary
        assert run.completed_observations == 0

    def test_credential_change_after_preflight_blocks_live_run(self, db, settings, monkeypatch):
        _bound_preflight(db, settings, monkeypatch)
        store_credential(db, "openai", SECRET, settings)
        summary = self._run(db, settings, monkeypatch)
        run = db.get(SuiteRun, summary.suite_run.id)
        assert run.status == "failed"
        assert "CREDENTIAL CHANGED SINCE PREFLIGHT" in run.error_summary
        assert db.query(Observation).filter(Observation.suite_run_id == run.id).count() == 0

    def test_ambiguous_blocks_live_run_without_invoking_adapter(self, db, settings, monkeypatch):
        monkeypatch.setenv("OPENAI_API_KEY", "sk-fake-env")
        seed_keychain(SECRET, settings)
        import endpoint_ledger.providers as providers
        import endpoint_ledger.runner.service as runner_mod

        invoked: list[str] = []

        class SpyingAdapter(FakeOpenAIAdapter):
            def invoke(self, *args, **kwargs):
                invoked.append("invoke")
                return super().invoke(*args, **kwargs)

        def fake_get_provider(name, settings=None, session=None):
            if name == "openai":
                return SpyingAdapter()
            from endpoint_ledger.providers.mock import MockProvider

            return MockProvider()

        monkeypatch.setattr(providers, "get_provider", fake_get_provider)
        monkeypatch.setattr(runner_mod, "get_provider", fake_get_provider)
        summary = self._run(db, settings, monkeypatch)
        run = db.get(SuiteRun, summary.suite_run.id)
        assert run.status == "failed"
        assert "select one explicitly" in run.error_summary
        assert invoked == []


class TestRejectIsAnnotationOnly:
    def _smoke(self, db, settings, monkeypatch):
        _fake_providers(monkeypatch)
        _bound_preflight(db, settings, monkeypatch)
        result = execute_qualification_smoke(db, settings)
        assert result["ok"] is True
        return result["run_id"]

    def test_reject_creates_annotation_and_changes_nothing_else(self, db, settings, monkeypatch):
        from endpoint_ledger.services.qualification_service import reject

        run_id = self._smoke(db, settings, monkeypatch)
        run = db.get(SuiteRun, run_id)
        before = {
            "status": run.status,
            "completed": run.completed_observations,
            "failed": run.failed_observations,
            "evidence": run.evidence_state,
            "observations": db.query(Observation).filter(Observation.suite_run_id == run_id).count(),
            "scores": db.query(Score).join(Observation).filter(Observation.suite_run_id == run_id).count(),
        }
        response = reject(db, run_id, "operational failure: account configuration (insufficient quota)")
        assert response["ok"] is True
        annotation = (
            db.query(RunAnnotation)
            .filter(
                RunAnnotation.suite_run_id == run_id,
                RunAnnotation.annotation_type == "qualification_rejection",
            )
            .one_or_none()
        )
        assert annotation is not None
        assert "operational failure" in annotation.notes
        db.refresh(run)
        assert run.status == before["status"]
        assert run.completed_observations == before["completed"]
        assert run.failed_observations == before["failed"]
        assert run.evidence_state == before["evidence"]
        assert db.query(Observation).filter(Observation.suite_run_id == run_id).count() == before["observations"]
        assert db.query(Score).join(Observation).filter(Observation.suite_run_id == run_id).count() == before["scores"]

    def test_review_page_offers_one_click_operational_rejection(self, db, settings, monkeypatch):
        run_id = self._smoke(db, settings, monkeypatch)
        body = _client().get(f"/baseline-ops/review/{run_id}").text
        assert "Reject as Operational Failure" in body

    def test_route_reject_posts_annotation_only(self, db, settings, monkeypatch):
        run_id = self._smoke(db, settings, monkeypatch)
        client = _client()
        token = _csrf(client)
        response = client.post(
            f"/baseline-ops/reject/{run_id}",
            data={
                "reason": "operational failure: account configuration (insufficient quota)",
                "csrf_token": token,
            },
            follow_redirects=False,
        )
        assert response.status_code == 303
        annotations = (
            db.query(RunAnnotation)
            .filter(
                RunAnnotation.suite_run_id == run_id,
                RunAnnotation.annotation_type == "qualification_rejection",
            )
            .all()
        )
        assert len(annotations) == 1
        assert "insufficient quota" in annotations[0].notes

    def test_run_14_rejection_adds_only_an_annotation(self, db):
        from endpoint_ledger.services.qualification_service import reject

        run = SuiteRun(
            id=14,
            suite_name="qualification-smoke-2026-08-10",
            status="completed_with_errors",
            total_expected_observations=6,
            completed_observations=6,
            failed_observations=1,
            runner_version="0.1.0",
            data_origin="live",
            evidence_state="exported",
        )
        db.add(run)
        db.commit()
        before = (run.status, run.completed_observations, run.failed_observations, run.evidence_state)

        result = reject(db, 14, "operational failure: account configuration")

        assert result["ok"] is True
        db.refresh(run)
        assert (run.status, run.completed_observations, run.failed_observations, run.evidence_state) == before
        annotation = db.query(RunAnnotation).filter(
            RunAnnotation.suite_run_id == 14,
            RunAnnotation.annotation_type == "qualification_rejection",
        ).one()
        assert "operational failure" in annotation.notes
