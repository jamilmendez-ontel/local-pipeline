"""Unit tests for the staging row builder. Run:
    cd swift_api_pipeline && venv/Scripts/python -m pytest test_calendar_events_transform.py -v
"""
from datetime import date
from calendar_events_transform import event_dates, build_row


def _allday(start, end):
    return {"id": "e1", "summary": "VL - Zeta - Luis",
            "start": {"date": start}, "end": {"date": end},
            "created": "2026-01-01T00:00:00Z", "updated": "2026-01-02T00:00:00Z",
            "creator": {"email": "a@ontel.co"}}


def test_event_dates_allday_inclusive():
    s, e, days, allday = event_dates(_allday("2026-03-02", "2026-03-05"))
    assert s == date(2026, 3, 2)
    assert e == date(2026, 3, 4)     # exclusive end -> inclusive
    assert days == 3
    assert allday is True


def test_build_row_maps_shape_and_kind():
    shape = {"event_kind": "leave", "leave_type": "VL", "team": "Zeta",
             "person": "Luis", "person_note": None, "rest_day_of_week": None,
             "confidence": 0.95, "parse_source": "deterministic", "needs_review": False}
    row = build_row(_allday("2026-03-02", "2026-03-03"), shape, "run-1")
    assert row["event_id"] == "e1"
    assert row["event_kind"] == "leave"
    assert row["leave_type"] == "VL"
    assert row["person"] == "Luis"
    assert row["run_id"] == "run-1"
    assert row["is_deleted"] is False
