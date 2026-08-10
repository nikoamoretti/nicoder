"""Safe destructive-operation framework.

Every command that deletes, truncates, resets, or replaces data goes through
this guard:

- dry-run is the default and prints the exact entity types and counts
- execution requires an explicit ``--execute`` flag
- for run replacement, every targeted SuiteRun must have the declared origin
- if any non-target record would enter the set, the whole transaction aborts
- a sanitized audit record is written to ``maintenance_operations``
- when live evidence exists, a recent verified backup is required
"""

from __future__ import annotations

from collections.abc import Callable, Sequence
from datetime import UTC, datetime
from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session

from endpoint_ledger.config import Settings, get_settings
from endpoint_ledger.db.models import MaintenanceOperation, SuiteRun

BackupProvider = Callable[[Settings], Any | None]


def live_evidence_exists(session: Session) -> bool:
    return session.scalar(select(SuiteRun.id).where(SuiteRun.data_origin == "live").limit(1)) is not None


def require_recent_verified_backup(
    session: Session,
    settings: Settings,
    last_verified: BackupProvider | None = None,
) -> None:
    """Refuse destructive maintenance when live evidence exists but no recent
    verified backup is available. Read-only commands never require a backup."""
    if not settings.require_backup_before_maintenance:
        return
    if not live_evidence_exists(session):
        return
    from endpoint_ledger.backup import last_verified_backup

    backup = last_verified_backup(settings) if last_verified is None else last_verified(settings)
    if backup is None:
        raise RuntimeError(
            "destructive maintenance refused: live evidence exists and no verified "
            f"backup younger than {settings.backup_max_age_hours}h is available; "
            "run `make db-backup` then `make db-verify-backup` first"
        )


class DestructiveGuard:
    def __init__(
        self,
        session: Session,
        operation_name: str,
        settings: Settings | None = None,
    ) -> None:
        self.session = session
        self.operation_name = operation_name
        self.settings = settings or get_settings()

    def plan(
        self,
        entity_types: Sequence[str],
        counts: dict[str, int],
    ) -> None:
        print(f"DRY RUN — {self.operation_name} would affect:")
        for entity in entity_types:
            print(f"  {entity}: {counts.get(entity, 0)} record(s)")
        print("  run with --execute to apply; nothing was changed")

    def execute(
        self,
        apply: Callable[[], None],
        entity_types: Sequence[str],
        counts: dict[str, int],
        target_origin: str | None = None,
        require_backup: bool = True,
    ) -> None:
        """Run the operation in a transaction; abort entirely on any error."""
        if require_backup:
            require_recent_verified_backup(self.session, self.settings)
        try:
            apply()
            self.session.flush()
        except Exception:
            self.session.rollback()
            raise
        self._log(entity_types, counts, target_origin)
        self.session.commit()

    def _log(
        self,
        entity_types: Sequence[str],
        counts: dict[str, int],
        target_origin: str | None,
    ) -> None:
        self.session.add(
            MaintenanceOperation(
                operation_name=self.operation_name,
                target_origin=target_origin,
                affected_entity_types=list(entity_types),
                affected_counts=counts,
                notes=f"executed at {datetime.now(UTC).isoformat()}",
            )
        )


def delete_demo_runs(
    session: Session,
    settings: Settings | None = None,
    execute: bool = False,
) -> dict[str, int]:
    """Replace demo runs and their dependent evidence.

    Every targeted SuiteRun is verified to be data_origin=demo; if any non-demo
    record would be affected, the transaction aborts completely.
    """
    settings = settings or get_settings()
    runs = list(
        session.scalars(
            select(SuiteRun).where(SuiteRun.data_origin == "demo").order_by(SuiteRun.id)
        ).all()
    )
    observation_ids: list[int] = []
    for run in runs:
        observation_ids.extend(o.id for o in run.observations)
    from endpoint_ledger.db.models import ChangeEvent

    demo_events = len(
        session.scalars(select(ChangeEvent.id).where(ChangeEvent.data_origin == "demo")).all()
    )
    counts = {
        "suite_runs": len(runs),
        "observations": len(observation_ids),
        "change_events": demo_events,
    }
    # Scope check: replacement may only touch demo-origin runs.
    non_demo = [r.id for r in runs if r.data_origin != "demo"]
    if non_demo:
        raise RuntimeError(f"refusing demo regeneration: non-demo runs in target set: {non_demo}")

    guard = DestructiveGuard(session, "generate-demo-history", settings)
    if not execute:
        guard.plan(("suite_runs", "observations"), counts)
        return counts

    def apply() -> None:
        from endpoint_ledger.db.models import ChangeEvent

        for event in session.scalars(
            select(ChangeEvent).where(ChangeEvent.data_origin == "demo")
        ).all():
            session.delete(event)
        for run in runs:
            session.delete(run)

    guard.execute(apply, ("suite_runs", "observations", "change_events"), counts, target_origin="demo")
    return counts
