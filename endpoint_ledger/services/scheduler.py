"""Database-backed scheduler for automated baseline collection.

A single scheduler loop runs inside the application. The database is the
source of truth; the schedule survives application restarts. Missed runs are
marked, never burst-executed.
"""

from __future__ import annotations

import os
import threading
import time
from datetime import UTC, datetime
from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session

from endpoint_ledger.config import Settings, get_settings
from endpoint_ledger.db.models import SchedulerState
from endpoint_ledger.services.collection_service import (
    execute_due_planned_runs,
    handle_missed_runs,
)

_LOCK_KEY = 9_000_001


def scheduler_tick(session: Session, settings: Settings | None = None) -> dict[str, Any]:
    """One scheduler cycle: mark missed runs, execute due runs."""
    settings = settings or get_settings()
    from sqlalchemy import text

    got_lock = session.execute(
        text("SELECT pg_try_advisory_lock(:key)"), {"key": _LOCK_KEY}
    ).scalar()
    if not got_lock:
        return {"ok": False, "reason": "scheduler lock held by another process"}
    try:
        handle_missed_runs(session)
        outcomes = execute_due_planned_runs(session, settings)
        state = session.scalar(select(SchedulerState).order_by(SchedulerState.id))
        if state is None:
            state = SchedulerState(hostname=os.uname().nodename)
            session.add(state)
        state.last_heartbeat_at = datetime.now(UTC)
        state.status = "running"
        session.commit()
        return {"ok": True, "processed": len(outcomes)}
    finally:
        session.execute(text("SELECT pg_advisory_unlock(:key)"), {"key": _LOCK_KEY})
        session.commit()


def start_scheduler(app_state_factory, settings: Settings | None = None) -> threading.Thread:
    """Start the background scheduler thread (daemon)."""
    settings = settings or get_settings()

    def _loop() -> None:
        while True:
            session = app_state_factory()
            try:
                scheduler_tick(session, settings)
            except Exception:
                session.rollback()
            finally:
                session.close()
            time.sleep(settings.scheduler_interval_seconds)

    thread = threading.Thread(target=_loop, daemon=True, name="endpoint-ledger-scheduler")
    thread.start()
    return thread
