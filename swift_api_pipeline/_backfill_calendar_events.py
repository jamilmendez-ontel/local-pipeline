# swift_api_pipeline/_backfill_calendar_events.py
"""One-off: re-clean all raw calendar events into data_staging.stg_calendar_events,
populating agent.calendar_summary_parse. Reads the latest raw payload per event_id.

Resolves each DISTINCT summary once (cache -> deterministic -> AI), not once per
event, so the slow part is bounded by the number of distinct summary strings
(a few thousand) rather than the event count (tens of thousands). Logs progress
so a long run is observable instead of silent.

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
PROGRESS_EVERY = 50                # log a line every N distinct summaries resolved


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

    # Resolve each DISTINCT summary once. This is the only expensive step
    # (each miss is a cache write, each low-confidence miss is an AI call), so
    # bounding it by distinct summaries instead of events is the speedup.
    distinct = sorted({(ev.get("summary") or "") for ev in events})
    total = len(distinct)
    logger.info(f"Resolving {total} distinct summaries (cache -> deterministic -> AI)...")

    shape_by_summary = {}
    ai_calls = 0
    for i, summary in enumerate(distinct, 1):
        shape = resolve(db, summary)
        shape_by_summary[summary] = shape
        if shape.get("parse_source") == "ai":
            ai_calls += 1
        if i % PROGRESS_EVERY == 0 or i == total:
            logger.info(f"  resolved {i}/{total} distinct summaries ({ai_calls} via AI so far)")

    logger.info(f"Resolution done: {total} distinct summaries, {ai_calls} needed AI. Loading rows...")

    # In-memory resolver: load_staging now does zero per-event DB lookups.
    def resolve_fn(_db, summary):
        return shape_by_summary.get(summary or "")

    counts = load_staging(db, run_id, events, resolve_fn=resolve_fn)
    logger.info(f"Backfill done: {counts}")
    close_db()


if __name__ == "__main__":
    main()
