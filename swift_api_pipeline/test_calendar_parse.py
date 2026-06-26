"""Unit tests for the deterministic calendar parser. Run:
    cd swift_api_pipeline && python test_calendar_parse.py
"""
from calendar_parse import normalize_separators


def test_normalize_digit_team_dash():
    # Defect 1: "CG1- Angelica" must become "CG1 - Angelica" so the split works.
    assert normalize_separators("VL - CG1- Angelica") == "VL - CG1 - Angelica"


def test_normalize_leading_dash():
    assert normalize_separators("SDL -CG1 - Tads") == "SDL - CG1 - Tads"


def test_normalize_letter_dash_unchanged_when_already_spaced():
    assert normalize_separators("VL - Zeta - Luis") == "VL - Zeta - Luis"
