# swift_api_pipeline/_backfill_calendar_events.py
"""One-off: re-clean all raw calendar events into data_staging.stg_calendar_events,
populating agent.calendar_summary_parse. Reads the latest raw payload per event_id.

Usage:
    cd swift_api_pipeline && venv/Scripts/python _backfill_calendar_events.py
"""
import uuid

from config import SCHEMA_RAW, get_db, close_db, setup_logging, get_logger
from calendar_parse_cache import resolve
from calendar_events_load import load_staging

setup_logging()
logger = get_logger("calendar_leave")

RAW_TABLE = "raw_calendar_leave"   # pre-cutover name


def main():
    db = get_db()
    run_id = f"backfill-{uuid.uuid4()}"
    rows = db.fetch(
        f"SELECT DISTINCT ON (event_id) event_id, data "
        f"FROM {SCHEMA_RAW}.{RAW_TABLE} "
        f"ORDER BY event_id, loaded_at DESC"
    )
    events = [r["data"] for r in (rows or [])]
    logger.info(f"Backfilling {len(events)} distinct events from {RAW_TABLE}")
    counts = load_staging(db, run_id, events, resolve_fn=resolve)
    logger.info(f"Backfill done: {counts}")
    close_db()


if __name__ == "__main__":
    main()
