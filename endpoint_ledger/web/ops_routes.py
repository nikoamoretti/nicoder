"""Baseline Operations dashboard routes.

Every state-changing action is a POST protected by CSRF. The page is a
state-based workflow: each state makes the next safe action obvious.
"""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Form, HTTPException, Request
from fastapi.responses import HTMLResponse, RedirectResponse
from sqlalchemy import select
from sqlalchemy.orm import Session

from endpoint_ledger.db.models import (
    BaselineCohort,
    CollectionPlan,
    PlannedCollectionRun,
    RunAnnotation,
    SuiteRun,
)
from endpoint_ledger.services import notifications
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
    validate_and_store_credential,
)
from endpoint_ledger.services.preflight_service import perform_preflight
from endpoint_ledger.services.qualification_service import (
    approve as approve_run,
)
from endpoint_ledger.services.qualification_service import (
    execute_qualification_smoke,
    review_report,
)
from endpoint_ledger.services.qualification_service import (
    reject as reject_run,
)
from endpoint_ledger.web.security import csrf_token_for

router = APIRouter()


def _db(request: Request) -> Session:
    factory = request.app.state.session_factory
    return factory()


def _templates(request: Request):
    return request.app.state.templates


def _state(request: Request, session: Session) -> dict[str, Any]:
    settings = request.app.state.settings
    credential = credential_status(session, "openai", settings)
    from endpoint_ledger.services.preflight_service import latest_preflight

    preflight = latest_preflight(session, "openai")
    preflight_ready = bool(
        preflight is not None
        and preflight.ready
        and preflight.binding_valid is not False
        and credential["active_fingerprint"] is not None
        and preflight.credential_fingerprint == credential["active_fingerprint"]
    )
    smoke = session.scalar(
        select(SuiteRun).where(
            SuiteRun.suite_name.like("qualification-smoke-%"),
            SuiteRun.data_origin == "live",
        ).order_by(SuiteRun.started_at.desc())
    )
    approved_annotation = None
    rejected_annotation = None
    if smoke is not None:
        approved_annotation = session.scalar(
            select(RunAnnotation).where(
                RunAnnotation.suite_run_id == smoke.id,
                RunAnnotation.annotation_type == "qualification_approval",
            )
        )
        rejected_annotation = session.scalar(
            select(RunAnnotation).where(
                RunAnnotation.suite_run_id == smoke.id,
                RunAnnotation.annotation_type == "qualification_rejection",
            )
        )
    plan = session.scalar(
        select(CollectionPlan).where(
            CollectionPlan.state.in_(["enabled", "paused", "stopping"])
        ).order_by(CollectionPlan.id.desc())
    )
    any_plan = session.scalar(select(CollectionPlan).order_by(CollectionPlan.id.desc()))
    cohorts = session.scalars(
        select(BaselineCohort).where(BaselineCohort.data_origin == "live")
    ).all()
    qualified = [c for c in cohorts if c.status == "qualified"]

    if not credential["configured"]:
        state = "credential_required"
    elif not preflight_ready:
        state = "preflight_blocked" if preflight is not None and not preflight.ready else "ready_for_preflight"
    elif smoke is None:
        state = "ready_for_qualification_smoke"
    elif smoke.status in ("running", "pending"):
        state = "qualification_smoke_running"
    elif rejected_annotation is not None:
        state = "qualification_smoke_rejected"
    elif approved_annotation is None:
        state = "qualification_smoke_ready_for_review"
    elif plan is not None and plan.state == "enabled":
        state = "collection_active"
    elif plan is not None and plan.state == "paused":
        state = "collection_paused"
    elif qualified:
        state = "baseline_qualified"
    elif any_plan is not None and any_plan.state == "disabled":
        state = (
            "collection_blocked"
            if any_plan.stop_reason and "block" in any_plan.stop_reason
            else "ready_to_schedule"
        )
    else:
        state = "ready_to_schedule"

    return {
        "state": state,
        "credential": credential,
        "preflight": preflight,
        "smoke": smoke,
        "approved_annotation": approved_annotation,
        "rejected_annotation": rejected_annotation,
        "plan": plan or any_plan,
        "qualified_cohorts": qualified,
        "csrf_token": csrf_token_for(request),
    }


@router.get("/baseline-ops", response_class=HTMLResponse)
def baseline_ops(request: Request):
    templates = _templates(request)
    session = _db(request)
    try:
        state = _state(request, session)
        from endpoint_ledger.backup import last_verified_backup
        from endpoint_ledger.db.models import SchedulerState

        backup = last_verified_backup(request.app.state.settings)
        scheduler = session.scalar(select(SchedulerState).order_by(SchedulerState.id))
        unread = notifications.unread_count(session)
        from endpoint_ledger.measurement.cohorts import CohortService

        service = CohortService(session, request.app.state.settings)
        cohort_reports = service.status_report(origin="live")

        return templates.TemplateResponse(
            request,
            "baseline_ops.html",
            {
                "state": state,
                "backup": backup,
                "scheduler": scheduler,
                "unread": unread,
                "cohort_reports": cohort_reports,
                "settings": request.app.state.settings,
            },
        )
    finally:
        session.close()


@router.post("/baseline-ops/credential")
def save_credential(
    request: Request,
    credential_value: str = Form(...),
):
    """POST-only, CSRF-protected API-key entry with validate-before-replace."""
    session = _db(request)
    try:
        settings = request.app.state.settings
        validation = validate_and_store_credential(session, "openai", credential_value, settings)
        notifications.notify(
            session,
            notifications.CATEGORY_CREDENTIAL_CHANGED,
            "API key saved and validated" if validation.get("ok") else "API key was not changed",
            message=validation.get("message", "API key validation did not complete"),
            severity="info" if validation.get("ok") else "warn",
        )
        return RedirectResponse("/baseline-ops", status_code=303)
    finally:
        session.close()


@router.post("/baseline-ops/credential/remove")
def remove_credential_action(request: Request, confirm: str = Form(...)):
    if confirm != "remove":
        raise HTTPException(status_code=400, detail="removal requires confirmation")
    session = _db(request)
    try:
        remove_credential(session, "openai", request.app.state.settings)
        notifications.notify(
            session,
            notifications.CATEGORY_CREDENTIAL_CHANGED,
            "API key removed",
            message="The dashboard-managed API key was removed.",
            severity="warn",
        )
        return RedirectResponse("/baseline-ops", status_code=303)
    finally:
        session.close()
@router.post("/baseline-ops/preflight")
def run_preflight_action(request: Request):
    session = _db(request)
    try:
        report = perform_preflight(
            session, request.app.state.settings,
            provider="openai", endpoint_filter="gpt-4o-mini",
            suite="smoke", repeats=5, cost_ceiling=1.0,
        )
        _ = report
        if not report["ready"]:
            notifications.notify(
                session,
                notifications.CATEGORY_PREFLIGHT_BLOCKED,
                "Preflight blocked",
                message="; ".join(report["refusals"]),
                severity="warn",
            )
        return RedirectResponse("/baseline-ops", status_code=303)
    finally:
        session.close()


@router.post("/baseline-ops/qualification-smoke")
def qualification_smoke_action(request: Request, confirm: str = Form(...)):
    if confirm != "run":
        raise HTTPException(status_code=400, detail="confirmation required")
    session = _db(request)
    try:
        result = execute_qualification_smoke(session, request.app.state.settings)
        _ = result
        return RedirectResponse("/baseline-ops", status_code=303)
    finally:
        session.close()


@router.get("/baseline-ops/review/{run_id}", response_class=HTMLResponse)
def qualification_review(request: Request, run_id: int):
    templates = _templates(request)
    session = _db(request)
    try:
        report = review_report(session, run_id)
        from endpoint_ledger.services.qualification_service import approval_checks

        errors = approval_checks(session, run_id)
        return templates.TemplateResponse(
            request,
            "qualification_review.html",
            {
                "report": report,
                "errors": errors,
                "csrf_token": csrf_token_for(request),
            },
        )
    finally:
        session.close()


@router.post("/baseline-ops/approve/{run_id}")
def approve_action(request: Request, run_id: int):
    session = _db(request)
    try:
        approve_run(session, run_id)
        return RedirectResponse("/baseline-ops", status_code=303)
    finally:
        session.close()


@router.post("/baseline-ops/reject/{run_id}")
def reject_action(request: Request, run_id: int, reason: str = Form(...)):
    session = _db(request)
    try:
        reject_run(session, run_id, reason)
        return RedirectResponse("/baseline-ops", status_code=303)
    finally:
        session.close()


@router.post("/baseline-ops/plan/create")
def create_plan_action(request: Request):
    session = _db(request)
    try:
        settings = request.app.state.settings
        from endpoint_ledger.db.models import ProviderEndpoint
        from endpoint_ledger.measurement.cohorts import CohortService

        endpoint = session.scalar(
            select(ProviderEndpoint).where(
                ProviderEndpoint.provider == "openai", ProviderEndpoint.enabled.is_(True)
            )
        )
        if endpoint is None:
            raise HTTPException(status_code=400, detail="no enabled openai endpoint")
        cohort_reports = CohortService(session, settings).status_report(provider="openai", origin="live")
        fingerprint = cohort_reports[0]["cohort"].fingerprint if cohort_reports else "pending"
        plan = create_plan(
            session, settings,
            provider="openai",
            endpoint_id=endpoint.id,
            suite_name="smoke",
            cohort_fingerprint=fingerprint,
            repeats_per_probe=5,
            qualification_run_id=None,
        )
        return RedirectResponse(f"/baseline-ops/plan/{plan.id}", status_code=303)
    finally:
        session.close()


@router.get("/baseline-ops/plan/{plan_id}", response_class=HTMLResponse)
def plan_detail(request: Request, plan_id: int):
    templates = _templates(request)
    session = _db(request)
    try:
        plan = session.get(CollectionPlan, plan_id)
        if plan is None:
            raise HTTPException(status_code=404, detail="plan not found")
        planned = session.scalars(
            select(PlannedCollectionRun).where(
                PlannedCollectionRun.collection_plan_id == plan_id
            ).order_by(PlannedCollectionRun.scheduled_for)
        ).all()
        return templates.TemplateResponse(
            request,
            "plan_detail.html",
            {
                "plan": plan,
                "planned": planned,
                "csrf_token": csrf_token_for(request),
                "settings": request.app.state.settings,
            },
        )
    finally:
        session.close()


@router.get("/baseline-ops/plan/{plan_id}/manual-run", response_class=HTMLResponse)
def manual_run_confirm(request: Request, plan_id: int):
    """Confirmation page for "Run One Additional Collection Now"."""
    import secrets

    templates = _templates(request)
    session = _db(request)
    try:
        from endpoint_ledger.services.collection_service import manual_run_confirmation

        data = manual_run_confirmation(session, request.app.state.settings, plan_id)
        if not data.get("ok", True):
            return RedirectResponse("/baseline-ops", status_code=303)
        data["csrf_token"] = csrf_token_for(request)
        data["idempotency_token"] = secrets.token_urlsafe(24)
        return templates.TemplateResponse(request, "manual_run_confirm.html", data)
    finally:
        session.close()


@router.post("/baseline-ops/plan/{plan_id}/manual-run")
def manual_run_action(
    request: Request,
    plan_id: int,
    confirm: str = Form(...),
    idempotency_token: str = Form(...),
):
    """Enqueue one additional manual collection run (execution is asynchronous)."""
    if confirm != "run":
        raise HTTPException(status_code=400, detail="confirmation required")
    session = _db(request)
    try:
        from endpoint_ledger.services.collection_service import request_manual_run

        result = request_manual_run(
            session, request.app.state.settings, plan_id, idempotency_token
        )
        if not result["ok"]:
            return RedirectResponse(
                f"/baseline-ops/plan/{plan_id}/manual-run", status_code=303
            )
        return RedirectResponse(
            f"/baseline-ops/manual-run/{result['planned_run_id']}", status_code=303
        )
    finally:
        session.close()


@router.get("/baseline-ops/manual-run/{planned_id}", response_class=HTMLResponse)
def manual_run_status(request: Request, planned_id: int):
    """Lightweight status page for a queued or executed manual run."""
    templates = _templates(request)
    session = _db(request)
    try:
        planned = session.get(PlannedCollectionRun, planned_id)
        if planned is None or planned.trigger_type != "manual_run_now":
            raise HTTPException(status_code=404, detail="manual run not found")
        return templates.TemplateResponse(
            request,
            "manual_run_status.html",
            {"planned": planned},
        )
    finally:
        session.close()


@router.post("/baseline-ops/plan/{plan_id}/enable")
def enable_action(request: Request, plan_id: int):
    session = _db(request)
    try:
        enable_plan(session, plan_id)
        return RedirectResponse("/baseline-ops", status_code=303)
    finally:
        session.close()


@router.post("/baseline-ops/plan/{plan_id}/pause")
def pause_action(request: Request, plan_id: int):
    session = _db(request)
    try:
        pause_plan(session, plan_id)
        return RedirectResponse("/baseline-ops", status_code=303)
    finally:
        session.close()


@router.post("/baseline-ops/plan/{plan_id}/resume")
def resume_action(request: Request, plan_id: int):
    session = _db(request)
    try:
        resume_plan(session, plan_id)
        return RedirectResponse("/baseline-ops", status_code=303)
    finally:
        session.close()


@router.post("/baseline-ops/plan/{plan_id}/stop")
def stop_action(request: Request, plan_id: int):
    session = _db(request)
    try:
        stop_after_current(session, plan_id)
        return RedirectResponse("/baseline-ops", status_code=303)
    finally:
        session.close()


@router.post("/baseline-ops/plan/{plan_id}/disable")
def disable_action(request: Request, plan_id: int):
    session = _db(request)
    try:
        disable_plan(session, plan_id)
        return RedirectResponse("/baseline-ops", status_code=303)
    finally:
        session.close()


@router.get("/notifications", response_class=HTMLResponse)
def notifications_page(request: Request):
    templates = _templates(request)
    session = _db(request)
    try:
        items = notifications.list_notifications(session)
        return templates.TemplateResponse(
            request,
            "notifications.html",
            {"items": items, "csrf_token": csrf_token_for(request)},
        )
    finally:
        session.close()


@router.post("/notifications/{notification_id}/read")
def mark_read_action(request: Request, notification_id: int):
    session = _db(request)
    try:
        notifications.mark_read(session, notification_id)
        return RedirectResponse("/notifications", status_code=303)
    finally:
        session.close()
