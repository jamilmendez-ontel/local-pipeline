"""Unit tests for calendar normalization pure functions. Run:
    cd swift_api_pipeline && venv/Scripts/python -m pytest test_calendar_normalize.py -v
"""
from calendar_normalize import (
    normalize_leave_type,
    normalize_team,
    build_employee_index,
    match_person_deterministic,
)

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


EMPLOYEES = [
    {"emp_id": "E1", "full_name": "Edward Cruz", "first_name": "Edward", "nickname": "Ed",
     "carrier_group": "CG1 - Verizon", "cluster": "Alpha"},
    {"emp_id": "E2", "full_name": "Edwin Santos", "first_name": "Edwin", "nickname": "Ed",
     "carrier_group": "CG2 - AT&T/DISH", "cluster": "Epsilon"},
    {"emp_id": "E3", "full_name": "Prince Uy", "first_name": "Prince", "nickname": None,
     "carrier_group": "Creatives", "cluster": None},
]
EMP_INDEX = build_employee_index(EMPLOYEES)
TEAM_MAP2 = {"cg1": ("CG1 - Verizon", "carrier_group"), "cg2": ("CG2 - AT&T/DISH", "carrier_group"),
             "crtv": ("Creatives", "carrier_group")}


def test_index_keys_lowercased():
    assert "ed" in EMP_INDEX and "prince" in EMP_INDEX and "edward cruz" in EMP_INDEX


def test_match_unique_first_name():
    emp, src = match_person_deterministic("Prince", "CRTV", EMP_INDEX, TEAM_MAP2)
    assert src == "exact" and emp["emp_id"] == "E3"


def test_match_ambiguous_nickname_disambiguated_by_team():
    emp, src = match_person_deterministic("Ed", "CG2", EMP_INDEX, TEAM_MAP2)
    assert src == "exact" and emp["emp_id"] == "E2"


def test_match_ambiguous_disambiguated_by_cluster():
    team_map = {"eps": ("Epsilon", "cluster")}
    emp, src = match_person_deterministic("Ed", "eps", EMP_INDEX, team_map)
    assert src == "exact" and emp["emp_id"] == "E2"


def test_match_ambiguous_without_team_signal():
    emp, src = match_person_deterministic("Ed", "Nonsense", EMP_INDEX, TEAM_MAP2)
    assert emp is None and src == "ambiguous"


def test_match_unmatched():
    emp, src = match_person_deterministic("Zzz", "CG1", EMP_INDEX, TEAM_MAP2)
    assert emp is None and src == "unmatched"


def test_match_none_person():
    emp, src = match_person_deterministic(None, "CG1", EMP_INDEX, TEAM_MAP2)
    assert emp is None and src == "unmatched"
