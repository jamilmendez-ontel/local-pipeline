"""Unit tests for calendar normalization pure functions. Run:
    cd swift_api_pipeline && venv/Scripts/python -m pytest test_calendar_normalize.py -v
"""
from calendar_normalize import normalize_leave_type

CODE_MAP = {
    "VL": ("Vacation Leave", "leave"),
    "SL": ("Sick Leave", "leave"),
    "UT": ("Undertime", "leave"),
    "RD": ("Rest Day", "rest"),
}


def test_leave_type_known():
    assert normalize_leave_type("VL", CODE_MAP) == ("Vacation Leave", "leave")


def test_leave_type_case_insensitive():
    assert normalize_leave_type("ww", {"WW": ("Weekend Work", "work")}) == ("Weekend Work", "work")


def test_leave_type_unknown_falls_back_to_raw():
    assert normalize_leave_type("LAC", CODE_MAP) == ("LAC", None)


def test_leave_type_compound():
    label, cat = normalize_leave_type("UT/SL", CODE_MAP)
    assert label == "Undertime + Sick Leave"
    assert cat == "compound"


def test_leave_type_compound_with_spaces():
    label, cat = normalize_leave_type("VL / LAC", CODE_MAP)
    assert label == "Vacation Leave + LAC"
    assert cat == "compound"


def test_leave_type_none():
    assert normalize_leave_type(None, CODE_MAP) == (None, None)
