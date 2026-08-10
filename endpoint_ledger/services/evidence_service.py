"""Evidence service: seal, export, and verify run evidence bundles."""

from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from sqlalchemy.orm import Session

from endpoint_ledger.config import Settings, get_settings
from endpoint_ledger.db.models import SuiteRun
from endpoint_ledger.evidence import mark_export_failed, mark_exported, seal_run
from endpoint_ledger.export import ExportInvalidError, export_run, verify_export
from endpoint_ledger.services import notifications


def seal_export_verify(
    session: Session,
    run: SuiteRun,
    settings: Settings | None = None,
) -> dict[str, Any]:
    """Seal, export, and verify a run's evidence bundle.

    On export failure the sealed database evidence is preserved and the run is
    marked export_failed; a notification is created and the error is returned.
    """
    settings = settings or get_settings()
    seal_run(session, run)
    session.commit()
    try:
        artifact = export_run(session, run, settings)
        verification = verify_export(Path(artifact.path))
        if not verification["ok"]:
            raise ExportInvalidError("; ".join(verification["errors"]))
        artifact.verification_status = "verified"
        artifact.verified_at = datetime.now(UTC)
        if run.data_origin == "live":
            mark_exported(session, run, artifact.sha256)
        session.commit()
        return {
            "ok": True,
            "path": artifact.path,
            "manifest_hash": artifact.sha256,
            "file_count": verification["file_count"],
        }
    except Exception as exc:
        session.rollback()
        session.expire_all()
        current_run = session.get(SuiteRun, run.id)
        if current_run is not None:
            mark_export_failed(session, current_run)
            session.commit()
            notifications.notify(
                session,
                notifications.CATEGORY_EXPORT_FAILED,
                f"Evidence export failed for run {current_run.id}",
                message="The sealed database evidence is preserved; durable archival failed. "
                f"Reason: {sanitize(str(exc))}",
                severity="error",
                suite_run_id=run.id,
            )
        return {"ok": False, "error": str(exc)}


def sanitize(message: str) -> str:
    from endpoint_ledger.sanitize import sanitize_message

    return sanitize_message(message)
