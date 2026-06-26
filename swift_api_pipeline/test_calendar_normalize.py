"""Unit tests for calendar normalization pure functions. Run:
    cd swift_api_pipeline && venv/Scripts/python -m pytest test_calendar_normalize.py -v
"""
from calendar_normalize import normalize_leave_type, normalize_team

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


TEAM_MAP = {
    "cg1": ("CG1 - Verizon", "carrier_group"),
    "alpha": ("Alpha", "cluster"),
    "trainee": (None, "status"),
}


def test_team_person_derived_uses_carrier_group():
    emp = {"carrier_group": "CG2 - AT&T/DISH", "cluster": "Epsilon"}
    assert normalize_team(emp, "Trainee", TEAM_MAP) == ("CG2 - AT&T/DISH", "carrier_group")


def test_team_fallback_to_label_when_no_emp():
    assert normalize_team(None, "CG1", TEAM_MAP) == ("CG1 - Verizon", "carrier_group")


def test_team_fallback_label_cluster_for_rd_row():
    assert normalize_team(None, "Alpha", TEAM_MAP) == ("Alpha", "cluster")


def test_team_unmapped_label_is_null():
    assert normalize_team(None, "Trainee", TEAM_MAP) == (None, "status")
    assert normalize_team(None, "Nonsense", TEAM_MAP) == (None, None)


def test_team_no_emp_no_team():
    assert normalize_team(None, None, TEAM_MAP) == (None, None)
