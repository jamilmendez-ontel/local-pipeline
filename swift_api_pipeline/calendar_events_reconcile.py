"""Reconcile staging against a live full Calendar listing: any active staged
event not present live is soft-deleted. Catches deletions that incremental
updatedMin sync can miss."""
import logging
from datetime import datetime, timezone

from config import SCHEMA_STAGING, retry_db

logger = logging.getLogger("calendar_leave")


def reconcile(db, live_event_ids: set) -> int:
    active = retry_db(
        lambda: db.fetch(
            f"SELECT event_id FROM {SCHEMA_STAGING}.stg_calendar_events "
            f"WHERE NOT is_deleted"),
        description="fetch active staged events")
    stale = [r["event_id"] for r in (active or []) if r["event_id"] not in live_event_ids]
    if not stale:
        return 0
    retry_db(
        lambda: db.execute(
            f"UPDATE {SCHEMA_STAGING}.stg_calendar_events "
            f"SET is_deleted = true, deleted_at = $2 WHERE event_id = ANY($1)",
            stale, datetime.now(timezone.utc)),
        description=f"reconcile soft-delete {len(stale)} events")
    logger.info(f"  Reconciliation soft-deleted {len(stale)} events")
    return len(stale)
