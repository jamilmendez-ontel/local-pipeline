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


def renormalize(db):
    """Re-enrich existing stg_calendar_events rows in place (no Calendar fetch)."""
    from calendar_lookups import load_lookups
    from calendar_normalize import normalize_leave_type, normalize_team
    from calendar_person_cache import resolve_person

    lookups = load_lookups(db)
    rows = db.fetch(
        "SELECT event_id, leave_type, team, person FROM data_staging.stg_calendar_events")
    logger.info(f"Renormalizing {len(rows)} rows")
    updates = []
    for r in rows:
        lt_norm, _cat = normalize_leave_type(r["leave_type"], lookups["code_map"])
        pm = resolve_person(db, r["person"], r["team"], lookups["emp_index"], lookups["team_map"])
        emp = lookups["emp_by_id"].get(pm["emp_id"]) if pm["emp_id"] else None
        team_norm, team_level = normalize_team(emp, r["team"], lookups["team_map"])
        updates.append((r["event_id"], lt_norm, team_norm, team_level,
                        pm["person_normalized"], pm["emp_id"], pm["match_source"]))

    sql = (
        "UPDATE data_staging.stg_calendar_events AS s SET "
        "  leave_type_normalized = v.ltn, team_normalized = v.tn, team_level = v.tl, "
        "  person_normalized = v.pn, emp_id = v.eid, person_match_source = v.pms "
        "FROM (VALUES ($1,$2,$3,$4,$5,$6,$7)) AS v(event_id, ltn, tn, tl, pn, eid, pms) "
        "WHERE s.event_id = v.event_id"
    )
    for i in range(0, len(updates), 500):
        batch = updates[i:i + 500]
        retry_db(lambda b=batch: db.executemany(sql, b),
                 description=f"renormalize batch {i//500+1}")
    logger.info(f"Renormalized {len(updates)} rows")


def main(full_refresh: bool = False, renorm: bool = False):
    db = get_db()
    if renorm:
        renormalize(db)
        close_db()
        return
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
    ap.add_argument("--renormalize", action="store_true")
    a = ap.parse_args()
    main(full_refresh=a.full_refresh, renorm=a.renormalize)
