"""Load conformed rows into data_staging.stg_calendar_events. Cancelled events
are soft-deleted (tombstoned), not skipped, so deletions propagate."""
import logging
from datetime import datetime, timezone

from config import SCHEMA_STAGING, retry_db
from calendar_events_transform import build_row
from calendar_lookups import load_lookups
from calendar_normalize import normalize_leave_type, normalize_team
from calendar_person_cache import resolve_person

logger = logging.getLogger("calendar_leave")

LOAD_BATCH_SIZE = 500

_UPSERT_COLS = [
    "event_id", "ical_uid", "summary_raw", "event_kind", "leave_type",
    "leave_type_normalized", "team", "team_normalized", "team_level",
    "person", "person_normalized", "emp_id", "person_match_source",
    "person_note", "person_note_normalized", "rest_day_of_week", "start_date",
    "end_date", "days", "is_all_day", "creator_email", "event_created",
    "event_updated", "parse_source", "parse_confidence", "needs_review",
    "is_deleted", "run_id",
]


def _enrich(db, shape, lookups):
    """Return the norm dict (leave/team/person normalized values) for one event.
    person_note_normalized is always None until Wave 2."""
    lt_norm, _cat = normalize_leave_type(shape.get("leave_type"), lookups["code_map"])
    pm = resolve_person(db, shape.get("person"), shape.get("team"),
                        lookups["emp_index"], lookups["team_map"])
    emp = lookups["emp_by_id"].get(pm["emp_id"]) if pm["emp_id"] else None
    team_norm, team_level = normalize_team(emp, shape.get("team"), lookups["team_map"])
    return {
        "leave_type_normalized": lt_norm,
        "team_normalized": team_norm,
        "team_level": team_level,
        "person_normalized": pm["person_normalized"],
        "emp_id": pm["emp_id"],
        "person_match_source": pm["match_source"],
        "person_note_normalized": None,   # filled in Wave 2
    }


# Columns the ON CONFLICT tail assigns explicitly, so they must NOT also appear
# in the dynamic EXCLUDED list (Postgres rejects assigning a column twice).
# event_id is the conflict key; is_deleted/deleted_at are reset to "alive" because
# re-seeing an event means it was un-cancelled.
_TAIL_MANAGED = {"event_id", "is_deleted"}


def _upsert_sql() -> str:
    cols = ", ".join(_UPSERT_COLS)
    ph = ", ".join(f"${i+1}" for i in range(len(_UPSERT_COLS)))
    updates = ", ".join(f"{c} = EXCLUDED.{c}" for c in _UPSERT_COLS if c not in _TAIL_MANAGED)
    return (
        f"INSERT INTO {SCHEMA_STAGING}.stg_calendar_events ({cols}, parsed_at, loaded_at) "
        f"VALUES ({ph}, now(), now()) "
        f"ON CONFLICT (event_id) DO UPDATE SET {updates}, "
        f"  is_deleted = false, deleted_at = NULL, parsed_at = now(), loaded_at = now()"
    )


def _tombstone_many(db, event_ids: list):
    """Soft-delete all given event_ids in one UPDATE. One round-trip regardless
    of count (a backfill replays thousands of historical cancellations; a
    per-event UPDATE there is thousands of round-trips)."""
    if not event_ids:
        return
    retry_db(
        lambda: db.execute(
            f"UPDATE {SCHEMA_STAGING}.stg_calendar_events "
            f"SET is_deleted = true, deleted_at = $2 WHERE event_id = ANY($1)",
            event_ids, datetime.now(timezone.utc),
        ),
        description=f"tombstone {len(event_ids)} cancelled events",
    )


def load_staging(db, run_id: str, events: list, resolve_fn) -> dict:
    upserted = tombstoned = skipped = 0
    rows = []
    cancelled_ids = []
    lookups = load_lookups(db)
    for ev in events:
        if ev.get("status") == "cancelled":
            eid = ev.get("id", "")
            if eid:
                cancelled_ids.append(eid)
            continue
        try:
            shape = resolve_fn(db, ev.get("summary") or "")
            norm = _enrich(db, shape, lookups)
            rows.append(build_row(ev, shape, run_id, norm))
        except Exception as e:
            skipped += 1
            logger.warning(f"  Skipped event {ev.get('id','?')}: {e}")

    _tombstone_many(db, cancelled_ids)
    tombstoned = len(cancelled_ids)

    sql = _upsert_sql()
    n_batches = (len(rows) + LOAD_BATCH_SIZE - 1) // LOAD_BATCH_SIZE
    for i in range(0, len(rows), LOAD_BATCH_SIZE):
        batch = rows[i:i + LOAD_BATCH_SIZE]
        tuples = [tuple(r[c] for c in _UPSERT_COLS) for r in batch]
        batch_no = i // LOAD_BATCH_SIZE + 1
        retry_db(lambda t=tuples: db.executemany(sql, t),
                 description=f"upsert stg_calendar_events batch {batch_no}")
        upserted += len(batch)
        logger.info(f"  upserted batch {batch_no}/{n_batches} ({upserted} rows)")

    logger.info(f"  Upserted {upserted}, tombstoned {tombstoned}, skipped {skipped}")
    return {"upserted": upserted, "tombstoned": tombstoned, "skipped": skipped}
