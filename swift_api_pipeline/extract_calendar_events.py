#!/usr/bin/env python3
"""Calendar Events pipeline (Phase 1 transform). Extract -> raw -> conformed
stg_calendar_events via cached parse, with soft-delete + reconciliation.

Usage:
    python extract_calendar_events.py                 # incremental
    python extract_calendar_events.py --full-refresh  # re-resolve all raw
"""
import uuid
import argparse
from datetime import datetime, timezone

from config import SCHEMA_RAW, get_db, close_db, retry_db, setup_logging, get_logger
from calendar_client import authenticate_calendar
from calendar_events_fetch import forward_time_max, watermark_from_raw
from calendar_events_load import load_staging
from calendar_events_reconcile import reconcile
from calendar_parse_cache import resolve

setup_logging()
logger = get_logger("calendar_leave")

CALENDAR_ID = "c_9b404e3738157b5b83e066ba4e0d2dcddbcb2b9bf60b4027620c1d939636c778@group.calendar.google.com"
TIME_MIN = "2024-01-01T00:00:00Z"


def _fetch(service, updated_min, time_max):
    params = {"calendarId": CALENDAR_ID, "maxResults": 2500, "singleEvents": True,
              "orderBy": "startTime", "timeMin": TIME_MIN, "timeMax": time_max}
    if updated_min:
        params["updatedMin"] = updated_min
    events, token = [], None
    while True:
        if token:
            params["pageToken"] = token
        res = service.events().list(**params).execute()
        events.extend(res.get("items", []))
        token = res.get("nextPageToken")
        if not token:
            break
    return events


def _load_raw(db, run_id, events):
    for i in range(0, len(events), 500):
        batch = events[i:i + 500]
        tuples = [(run_id, ev.get("id", ""), ev) for ev in batch]
        retry_db(lambda t=tuples: db.executemany(
            f"INSERT INTO {SCHEMA_RAW}.raw_calendar_events (run_id, event_id, data) "
            f"VALUES ($1,$2,$3)", t), description=f"insert raw batch {i//500+1}")


def main(full_refresh: bool = False):
    db = get_db()
    run_id = str(uuid.uuid4())
    service = authenticate_calendar()
    time_max = forward_time_max(datetime.now(timezone.utc).date())
    updated_min = None if full_refresh else watermark_from_raw(db)

    events = _fetch(service, updated_min, time_max)
    logger.info(f"Fetched {len(events)} events (full_refresh={full_refresh})")
    if events:
        _load_raw(db, run_id, events)
        counts = load_staging(db, run_id, events, resolve_fn=resolve)
        logger.info(f"Load: {counts}")

    # Reconcile on full listings only (updated_min is None == full window),
    # and never on an empty listing (a transient empty API response must not
    # soft-delete everything).
    if updated_min is None and events:
        live_ids = {ev.get("id") for ev in events if ev.get("id")}
        reconcile(db, live_ids)

    close_db()


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--full-refresh", action="store_true")
    main(full_refresh=ap.parse_args().full_refresh)
