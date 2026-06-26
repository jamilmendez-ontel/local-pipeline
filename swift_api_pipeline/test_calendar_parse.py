"""Unit tests for the deterministic calendar parser. Run:
    cd swift_api_pipeline && python -m pytest test_calendar_parse.py -v
"""
from calendar_parse import normalize_separators, canonical_weekday, split_note, classify_kind


def test_normalize_digit_team_dash():
    # Defect 1: "CG1- Angelica" must become "CG1 - Angelica" so the split works.
    assert normalize_separators("VL - CG1- Angelica") == "VL - CG1 - Angelica"


def test_normalize_leading_dash():
    assert normalize_separators("SDL -CG1 - Tads") == "SDL - CG1 - Tads"


def test_normalize_letter_dash_unchanged_when_already_spaced():
    assert normalize_separators("VL - Zeta - Luis") == "VL - Zeta - Luis"


def test_canonical_weekday_variants():
    assert canonical_weekday("Tue") == "Tue"
    assert canonical_weekday("Tues") == "Tue"
    assert canonical_weekday("Tuesday") == "Tue"
    assert canonical_weekday("WEDNESDAY") == "Wed"
    assert canonical_weekday("Thurs") == "Thu"
    assert canonical_weekday("Merj") is None


def test_split_note_parenthetical():
    assert split_note("Chesca (3pm onwards)") == ("Chesca", "3pm onwards")


def test_split_note_unparenthesized_trailing():
    # Defect 5: "Mik - In by 12PM" -> person "Mik", note "In by 12PM".
    assert split_note("Mik - In by 12PM") == ("Mik", "In by 12PM")


def test_split_note_plain_name():
    assert split_note("Luis") == ("Luis", None)


def test_classify_holiday():
    assert classify_kind("PH Holiday: Labor Day", "PH") == "holiday"
    assert classify_kind("Christmas Holiday (Company-Wide)", None) == "holiday"


def test_classify_birthday():
    assert classify_kind("Ced's Birthday!", None) == "birthday"


def test_classify_training():
    assert classify_kind("AT&T COP Refresher Course", None) == "training"
    assert classify_kind("Swift Projects Training Walkthrough", None) == "training"


def test_classify_leave_from_known_code():
    assert classify_kind("VL - Zeta - Luis", "VL") == "leave"


def test_classify_other_blank():
    assert classify_kind("", None) == "other"
    assert classify_kind("230701\tRoel Longcop Annual Performance Evaluation", None) == "other"
