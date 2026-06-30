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
logger = get_logger("calendar_events")

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
    """Re-enrich existing stg_calendar_events rows in place (no Calendar fetch).
    Bulk-loads the person-match cache and writes updates in one round-trip to
    avoid a per-row loop against the remote DB (which times out at scale)."""
    from calendar_lookups import load_lookups
    from calendar_normalize import (normalize_leave_type, normalize_team,
                                     match_person_deterministic)
    from calendar_person_cache import extract_person_with_ai

    lookups = load_lookups(db)
    rows = db.fetch(
        "SELECT event_id, leave_type, team, person FROM data_staging.stg_calendar_events")
    logger.info(f"Renormalizing {len(rows)} rows")
    if not rows:
        logger.warning("No rows in stg_calendar_events; nothing to renormalize")
        return

    cache = {}
    for c in db.fetch(
            "SELECT person_raw, team_raw, emp_id, person_normalized, confidence, "
            "match_source FROM agent.calendar_person_match"):
        cache[(c["person_raw"], c["team_raw"])] = {
            "emp_id": c["emp_id"], "person_normalized": c["person_normalized"],
            "confidence": c["confidence"], "match_source": c["match_source"]}

    candidate_names = sorted({e.get("full_name")
                              for cands in lookups["emp_index"].values()
                              for e in cands if e.get("full_name")})
    new_entries = {}

    def _resolve(person, team):
        if not person or not person.strip():
            return {"emp_id": None, "person_normalized": None,
                    "confidence": 0.0, "match_source": "unmatched"}
        key = (person, team or "")
        if key in cache:
            return cache[key]
        if key in new_entries:
            return new_entries[key]
        emp, _src = match_person_deterministic(
            person, team, lookups["emp_index"], lookups["team_map"])
        if emp is not None:
            res = {"emp_id": emp.get("emp_id"), "person_normalized": emp.get("full_name"),
                   "confidence": 1.0, "match_source": "exact"}
        else:
            ai = extract_person_with_ai(person, team, candidate_names)
            if ai and ai.get("person_normalized"):
                chosen = ai["person_normalized"]
                matches = lookups["emp_index"].get(chosen.strip().lower(), [])
                res = {"emp_id": matches[0].get("emp_id") if matches else None,
                       "person_normalized": chosen,
                       "confidence": ai.get("confidence", 0.5), "match_source": "ai"}
            else:
                res = {"emp_id": None, "person_normalized": None,
                       "confidence": 0.0, "match_source": "unmatched"}
        new_entries[key] = res
        return res

    event_ids, ltns, tns, tls, pns, eids, pmss = [], [], [], [], [], [], []
    for r in rows:
        lt_norm, _cat = normalize_leave_type(r["leave_type"], lookups["code_map"])
        pm = _resolve(r["person"], r["team"])
        emp = lookups["emp_by_id"].get(pm["emp_id"]) if pm["emp_id"] else None
        team_norm, team_level = normalize_team(emp, r["team"], lookups["team_map"])
        event_ids.append(r["event_id"]); ltns.append(lt_norm); tns.append(team_norm)
        tls.append(team_level); pns.append(pm["person_normalized"])
        eids.append(pm["emp_id"]); pmss.append(pm["match_source"])

    if new_entries:
        cache_sql = (
            "INSERT INTO agent.calendar_person_match "
            "(person_raw, team_raw, emp_id, person_normalized, confidence, match_source) "
            "VALUES ($1,$2,$3,$4,$5,$6) "
            "ON CONFLICT (person_raw, team_raw) DO UPDATE SET "
            "  emp_id=EXCLUDED.emp_id, person_normalized=EXCLUDED.person_normalized, "
            "  confidence=EXCLUDED.confidence, match_source=EXCLUDED.match_source, "
            "  resolved_at=now()")
        cache_params = [(p, t, v["emp_id"], v["person_normalized"], v["confidence"],
                         v["match_source"]) for (p, t), v in new_entries.items()]
        retry_db(lambda: db.executemany(cache_sql, cache_params),
                 description=f"write {len(cache_params)} person-match cache rows")

    update_sql = (
        "UPDATE data_staging.stg_calendar_events AS s SET "
        "  leave_type_normalized = d.ltn, team_normalized = d.tn, team_level = d.tl, "
        "  person_normalized = d.pn, emp_id = d.eid, person_match_source = d.pms "
        "FROM unnest($1::text[], $2::text[], $3::text[], $4::text[], $5::text[], "
        "            $6::text[], $7::text[]) "
        "  AS d(event_id, ltn, tn, tl, pn, eid, pms) "
        "WHERE s.event_id = d.event_id")
    retry_db(lambda: db.execute(update_sql, event_ids, ltns, tns, tls, pns, eids, pmss),
             description=f"bulk renormalize {len(event_ids)} rows")
    logger.info(f"Renormalized {len(event_ids)} rows "
                f"({len(new_entries)} new person-match entries)")


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
