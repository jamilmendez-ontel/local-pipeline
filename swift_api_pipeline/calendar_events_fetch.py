"""Fetch-window helpers: forward 12-month cap and a raw-sourced incremental
watermark with a safety overlap (re-processing is idempotent; skipping loses data)."""
from datetime import date, datetime, timezone, timedelta

from config import SCHEMA_RAW

WATERMARK_OVERLAP_SECONDS = 60


def forward_time_max(today: date) -> str:
    """RFC3339 timestamp 12 months ahead of `today` (forward extract cap)."""
    y, m = today.year, today.month + 12
    y += (m - 1) // 12
    m = (m - 1) % 12 + 1
    day = min(today.day, 28)        # avoid month-length edge cases
    return datetime(y, m, day, tzinfo=timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.000Z")


def watermark_from_raw(db) -> str | None:
    """Max event 'updated' seen in raw, minus an overlap. Sourced from raw (not
    staging) so staging can be rebuilt without disturbing incremental sync."""
    ts = db.fetchval(
        f"SELECT max((data->>'updated')::timestamptz) FROM {SCHEMA_RAW}.raw_calendar_events"
    )
    if ts is None:
        return None
    ts = ts.astimezone(timezone.utc) - timedelta(seconds=WATERMARK_OVERLAP_SECONDS)
    return ts.strftime("%Y-%m-%dT%H:%M:%S.%f")[:-3] + "Z"
