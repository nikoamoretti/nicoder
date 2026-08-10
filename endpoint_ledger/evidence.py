"""Evidence lifecycle for suite runs.

Live evidence becomes append-only once a run reaches a terminal state and is
sealed. Sealing is enforced at the database level (triggers in migration 0003);
this module provides the application-level helpers and the domain error.
"""

from __future__ import annotations

from datetime import UTC, datetime

from sqlalchemy.orm import Session

from endpoint_ledger.db.models import SuiteRun

EVIDENCE_STATE_OPEN = "open"
EVIDENCE_STATE_SEALED = "sealed"
EVIDENCE_STATE_EXPORTED = "exported"
EVIDENCE_STATE_EXPORT_FAILED = "export_failed"

EVIDENCE_STATES = (
    EVIDENCE_STATE_OPEN,
    EVIDENCE_STATE_SEALED,
    EVIDENCE_STATE_EXPORTED,
    EVIDENCE_STATE_EXPORT_FAILED,
)

TERMINAL_STATUSES = ("completed", "completed_with_errors", "failed", "cancelled")


class SealedEvidenceError(RuntimeError):
    """Raised when code attempts to modify or delete sealed live evidence."""


def is_live(run: SuiteRun) -> bool:
    return run.data_origin == "live"


def is_sealed(run: SuiteRun) -> bool:
    return is_live(run) and run.evidence_state != EVIDENCE_STATE_OPEN


def seal_run(session: Session, run: SuiteRun) -> None:
    """Seal a terminal live run. Idempotent; no-op for non-live runs."""
    if run.status not in TERMINAL_STATUSES:
        raise ValueError(
            f"cannot seal a run in status {run.status!r}; terminal status required"
        )
    if not is_live(run):
        return
    if run.evidence_state == EVIDENCE_STATE_OPEN:
        run.evidence_state = EVIDENCE_STATE_SEALED
        run.sealed_at = datetime.now(UTC)
        session.flush()


def mark_exported(
    session: Session,
    run: SuiteRun,
    manifest_hash: str,
) -> None:
    """Record a verified export on a sealed run."""
    if run.evidence_state not in (EVIDENCE_STATE_SEALED, EVIDENCE_STATE_EXPORTED):
        raise SealedEvidenceError(
            f"run {run.id} is {run.evidence_state!r}; exports require a sealed run"
        )
    run.evidence_state = EVIDENCE_STATE_EXPORTED
    run.evidence_manifest_hash = manifest_hash
    run.evidence_exported_at = datetime.now(UTC)
    session.flush()


def mark_export_failed(session: Session, run: SuiteRun) -> None:
    """Record an export failure. The run stays sealed; evidence is preserved."""
    if run.evidence_state == EVIDENCE_STATE_SEALED:
        run.evidence_state = EVIDENCE_STATE_EXPORT_FAILED
        session.flush()
