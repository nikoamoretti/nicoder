"""In-product automation notifications."""

from __future__ import annotations

from datetime import UTC, datetime

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from endpoint_ledger.db.models import AutomationNotification

CATEGORY_CREDENTIAL_MISSING = "credential_missing"
CATEGORY_PREFLIGHT_BLOCKED = "preflight_blocked"
CATEGORY_QUALIFICATION_SMOKE_COMPLETED = "qualification_smoke_completed"
CATEGORY_QUALIFICATION_AWAITING_REVIEW = "qualification_smoke_awaiting_review"
CATEGORY_QUALIFICATION_APPROVED = "qualification_approved"
CATEGORY_QUALIFICATION_REJECTED = "qualification_rejected"
CATEGORY_COLLECTION_ENABLED = "collection_enabled"
CATEGORY_RUN_COMPLETED = "scheduled_run_completed"
CATEGORY_RUN_PARTIAL_FAILURE = "scheduled_run_partial_failure"
CATEGORY_RUN_BLOCKED = "scheduled_run_blocked"
CATEGORY_BACKUP_FAILED = "backup_failed"
CATEGORY_EXPORT_FAILED = "evidence_export_failed"
CATEGORY_MODEL_RESOLUTION = "model_resolution_changed"
CATEGORY_COLLECTION_PAUSED = "collection_paused"
CATEGORY_BASELINE_QUALIFIED = "baseline_qualified"
CATEGORY_COLLECTION_COMPLETED_UNQUALIFIED = "collection_completed_unqualified"
CATEGORY_BEHAVIORAL_CHANGE = "possible_behavioral_change"
CATEGORY_COLLECTION_DISABLED = "collection_disabled"
CATEGORY_MISSED_RUN = "missed_run"
CATEGORY_MANUAL_REQUESTED = "manual_collection_requested"
CATEGORY_MANUAL_BLOCKED = "manual_collection_blocked"
CATEGORY_MANUAL_STARTED = "manual_collection_started"
CATEGORY_MANUAL_COMPLETED = "manual_collection_completed"
CATEGORY_MANUAL_PARTIAL_FAILURE = "manual_collection_completed_with_errors"
CATEGORY_MANUAL_EXPORT_FAILED = "manual_collection_export_failed"
CATEGORY_MANUAL_MODEL_RESOLUTION = "manual_collection_model_resolution"
CATEGORY_MANUAL_QUALIFIED = "manual_collection_baseline_qualified"


def notify(
    session: Session,
    category: str,
    title: str,
    message: str = "",
    severity: str = "info",
    collection_plan_id: int | None = None,
    suite_run_id: int | None = None,
) -> AutomationNotification:
    notification = AutomationNotification(
        category=category,
        title=title,
        message=message,
        severity=severity,
        collection_plan_id=collection_plan_id,
        suite_run_id=suite_run_id,
    )
    session.add(notification)
    session.commit()
    return notification


def unread_count(session: Session) -> int:
    return db_count(session)


def db_count(session: Session) -> int:
    return (
        session.scalar(
            select(func.count()).select_from(AutomationNotification).where(
                AutomationNotification.read_at.is_(None)
            )
        )
        or 0
    )


def list_notifications(session: Session, limit: int = 50) -> list[AutomationNotification]:
    return list(
        session.scalars(
            select(AutomationNotification)
            .order_by(AutomationNotification.created_at.desc())
            .limit(limit)
        ).all()
    )


def mark_read(session: Session, notification_id: int) -> None:
    notification = session.get(AutomationNotification, notification_id)
    if notification is not None and notification.read_at is None:
        notification.read_at = datetime.now(UTC)
        session.commit()
