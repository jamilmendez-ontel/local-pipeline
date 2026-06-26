"""Unit tests for the deterministic calendar parser. Run:
    cd swift_api_pipeline && python test_calendar_parse.py
"""
from calendar_parse import normalize_separators, canonical_weekday, split_note


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
