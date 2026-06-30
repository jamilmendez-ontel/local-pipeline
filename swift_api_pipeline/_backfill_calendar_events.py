# swift_api_pipeline/_backfill_calendar_events.py
"""One-off: re-clean all raw calendar events into data_staging.stg_calendar_events,
populating agent.calendar_summary_parse.

Performance: the live resolve() path does one DB round-trip per summary, which is
fine for incremental runs (a handful of new events) but ~0.9s/summary against the
Singapore DB makes a full backfill of ~1.6k distinct summaries blow past 25 min.
So this backfill bulk-loads the whole parse cache in ONE query, resolves every
distinct summary in memory (deterministic, then AI only for true misses), then
bulk-writes new cache rows and batch-upserts the event rows. That turns thousands
of round-trips into a few queries; the only slow part left is the AI calls for
genuinely-new messy summaries.

Usage:
    cd swift_api_pipeline && venv/Scripts/python _backfill_calendar_events.py
"""
import uuid

from config import SCHEMA_RAW, get_db, close_db, retry_db, setup_logging, get_logger
from calendar_parse import deterministic_parse, CONFIDENCE_GATE
from calendar_ai_extract import extract_with_ai, MODEL, PROMPT_VERSION
from calendar_parse_cache import summary_key, _row_to_shape
from calendar_events_load import load_staging

setup_logging()
logger = get_logger("calendar_events")

RAW_TABLE = "raw_calendar_events"   # renamed from raw_calendar_leave at 2026-06-26 cutover
PROGRESS_EVERY = 200               # log a line every N distinct summaries resolved


def _bulk_load_cache(db) -> dict:
    """Load the entire parse cache into a dict keyed by summary_key (one query)."""
    rows = retry_db(
        lambda: db.fetch("SELECT * FROM agent.calendar_summary_parse"),
        description="bulk-load parse cache",
    )
    return {r["summary_key"]: _row_to_shape(r) for r in (rows or [])}


def _bulk_write_cache(db, new_entries: list):
    """Write new cache entries (key, shape) in batched executemany calls."""
    sql = (
        "INSERT INTO agent.calendar_summary_parse "
        "(summary_key, event_kind, leave_type, team, person, person_note, "
        " rest_day_of_week, confidence, parse_source, needs_review, model, prompt_version) "
        "VALUES ($1,$2,$3,$4,$5,$6,$7,$8,$9,$10,$11,$12) "
        "ON CONFLICT (summary_key) DO UPDATE SET "
        "  event_kind=EXCLUDED.event_kind, leave_type=EXCLUDED.leave_type, "
        "  team=EXCLUDED.team, person=EXCLUDED.person, person_note=EXCLUDED.person_note, "
        "  rest_day_of_week=EXCLUDED.rest_day_of_week, confidence=EXCLUDED.confidence, "
        "  parse_source=EXCLUDED.parse_source, needs_review=EXCLUDED.needs_review, "
        "  model=EXCLUDED.model, prompt_version=EXCLUDED.prompt_version, extracted_at=now()"
    )
    tuples = [
        (key, s["event_kind"], s["leave_type"], s["team"], s["person"],
         s["person_note"], s["rest_day_of_week"], s["confidence"],
         s["parse_source"], s["needs_review"],
         MODEL if s["parse_source"] == "ai" else None,
         PROMPT_VERSION if s["parse_source"] == "ai" else None)
        for key, s in new_entries
    ]
    for i in range(0, len(tuples), 500):
        chunk = tuples[i:i + 500]
        retry_db(lambda c=chunk: db.executemany(sql, c),
                 description=f"bulk-write cache chunk {i // 500 + 1}")


def main():
    db = get_db()
    run_id = f"backfill-{uuid.uuid4()}"

    rows = retry_db(
        lambda: db.fetch(
            f"SELECT DISTINCT ON (event_id) event_id, data "
            f"FROM {SCHEMA_RAW}.{RAW_TABLE} "
            f"ORDER BY event_id, loaded_at DESC"),
        description="load distinct raw events",
    )
    events = [r["data"] for r in (rows or [])]
    logger.info(f"Backfilling {len(events)} distinct events from {RAW_TABLE}")

    # One query: pull the whole cache into memory.
    cache = _bulk_load_cache(db)
    logger.info(f"Loaded {len(cache)} cached parses")

    # Resolve every distinct summary in memory. DB is touched only for true misses
    # (and only in bulk, at the end).
    distinct = sorted({(ev.get("summary") or "") for ev in events})
    total = len(distinct)
    logger.info(f"Resolving {total} distinct summaries in memory...")

    shape_by_summary = {}
    new_entries = []
    ai_calls = 0
    for i, summary in enumerate(distinct, 1):
        key = summary_key(summary)
        shape = cache.get(key)
        if shape is None:
            shape = deterministic_parse(summary)
            if shape["confidence"] < CONFIDENCE_GATE:
                shape = extract_with_ai(summary)
                ai_calls += 1
            cache[key] = shape
            new_entries.append((key, shape))
        shape_by_summary[summary] = shape
        if i % PROGRESS_EVERY == 0 or i == total:
            logger.info(f"  resolved {i}/{total} ({len(new_entries)} new, {ai_calls} via AI)")

    logger.info(f"Resolution done: {len(new_entries)} new cache entries ({ai_calls} via AI). "
                f"Writing cache + loading rows...")
    if new_entries:
        _bulk_write_cache(db, new_entries)
        logger.info(f"  wrote {len(new_entries)} new cache entries")

    # In-memory resolver: load_staging does zero per-event DB lookups.
    def resolve_fn(_db, summary):
        return shape_by_summary.get(summary or "")

    counts = load_staging(db, run_id, events, resolve_fn=resolve_fn)
    logger.info(f"Backfill done: {counts}")
    close_db()


if __name__ == "__main__":
    main()
