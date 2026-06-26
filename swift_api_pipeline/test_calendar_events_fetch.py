"""Tests for fetch-window helpers. Run:
    cd swift_api_pipeline && venv/Scripts/python -m pytest test_calendar_events_fetch.py -v
"""
from datetime import date, datetime, timezone
from calendar_events_fetch import forward_time_max, watermark_from_raw


def test_forward_time_max_is_12_months_out():
    assert forward_time_max(date(2026, 6, 25)).startswith("2027-06-25")


class FakeDB:
    def __init__(self, ts):
        self._ts = ts

    def fetchval(self, query, *args, **kw):
        return self._ts


def test_watermark_applies_overlap_and_full_precision():
    ts = datetime(2026, 6, 25, 10, 30, 15, 500000, tzinfo=timezone.utc)
    out = watermark_from_raw(FakeDB(ts))
    # 60s overlap subtracted -> 10:29:15, millisecond precision retained
    assert out.startswith("2026-06-25T10:29:15")


def test_watermark_none_when_empty():
    assert watermark_from_raw(FakeDB(None)) is None
