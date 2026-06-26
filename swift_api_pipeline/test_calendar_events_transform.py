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


def _norm_none():
    return {"leave_type_normalized": None, "team_normalized": None, "team_level": None,
            "person_normalized": None, "emp_id": None, "person_match_source": None,
            "person_note_normalized": None}


def test_build_row_maps_shape_and_kind():
    shape = {"event_kind": "leave", "leave_type": "VL", "team": "Zeta",
             "person": "Luis", "person_note": None, "rest_day_of_week": None,
             "confidence": 0.95, "parse_source": "deterministic", "needs_review": False}
    row = build_row(_allday("2026-03-02", "2026-03-03"), shape, "run-1", _norm_none())
    assert row["event_id"] == "e1"
    assert row["event_kind"] == "leave"
    assert row["leave_type"] == "VL"
    assert row["person"] == "Luis"
    assert row["run_id"] == "run-1"
    assert row["is_deleted"] is False


def test_build_row_includes_normalized_fields():
    ev = {"id": "x1", "summary": "VL - CG1 - Ed",
          "start": {"date": "2026-06-26"}, "end": {"date": "2026-06-27"}}
    shape = {"event_kind": "leave", "leave_type": "VL", "team": "CG1", "person": "Ed",
             "person_note": None, "rest_day_of_week": None, "parse_source": "deterministic",
             "confidence": 0.95, "needs_review": False}
    norm = {"leave_type_normalized": "Vacation Leave", "team_normalized": "CG1 - Verizon",
            "team_level": "carrier_group", "person_normalized": "Edward Cruz", "emp_id": "E1",
            "person_match_source": "exact", "person_note_normalized": None}
    row = build_row(ev, shape, "run1", norm)
    assert row["leave_type_normalized"] == "Vacation Leave"
    assert row["team_normalized"] == "CG1 - Verizon"
    assert row["team_level"] == "carrier_group"
    assert row["person_normalized"] == "Edward Cruz"
    assert row["emp_id"] == "E1"
    assert row["person_match_source"] == "exact"
    assert row["leave_type"] == "VL"      # raw preserved
