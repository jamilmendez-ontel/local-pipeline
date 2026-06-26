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


def test_classify_birthday_leave_is_not_birthday_noise():
    # "Birthday Leave" is the BL leave type, not a birthday marker.
    assert classify_kind("Lam - Birthday Leave", None) != "birthday"


def test_classify_training():
    assert classify_kind("AT&T COP Refresher Course", None) == "training"
    assert classify_kind("Swift Projects Training Walkthrough", None) == "training"


def test_classify_leave_from_known_code():
    assert classify_kind("VL - Zeta - Luis", "VL") == "leave"


def test_classify_other_blank():
    assert classify_kind("", None) == "other"
    assert classify_kind("230701\tRoel Longcop Annual Performance Evaluation", None) == "other"


from calendar_parse import deterministic_parse, CONFIDENCE_GATE


def test_parse_clean_three_part_high_confidence():
    r = deterministic_parse("VL - Zeta - Luis")
    assert r["event_kind"] == "leave"
    assert r["leave_type"] == "VL"
    assert r["team"] == "Zeta"
    assert r["person"] == "Luis"
    assert r["rest_day_of_week"] is None
    assert r["confidence"] >= CONFIDENCE_GATE
    assert r["parse_source"] == "deterministic"
    assert r["needs_review"] is False


def test_parse_digit_team_now_splits_clean():
    r = deterministic_parse("VL - CG1- Angelica")
    assert r["event_kind"] == "leave"
    assert r["leave_type"] == "VL"
    assert r["team"] == "CG1"
    assert r["person"] == "Angelica"
    assert r["confidence"] >= CONFIDENCE_GATE


def test_parse_rest_day_weekday_to_field_not_person():
    r = deterministic_parse("RD - Alpha - Fri")
    assert r["event_kind"] == "leave"
    assert r["leave_type"] == "RD"
    assert r["team"] == "Alpha"
    assert r["person"] is None
    assert r["rest_day_of_week"] == "Fri"
    assert r["confidence"] >= CONFIDENCE_GATE


def test_parse_underscore_low_confidence():
    # Defect 3: "VL_CRTV_Nicolai" must NOT confidently land in leave_type.
    r = deterministic_parse("VL_CRTV_Nicolai")
    assert r["confidence"] < CONFIDENCE_GATE


def test_parse_no_separator_noise_low_or_classified():
    r = deterministic_parse("Ced's Birthday!")
    assert r["event_kind"] == "birthday"
    # not leave, and leave_type must be None for a non-leave kind
    assert r["leave_type"] is None
    assert r["rest_day_of_week"] is None
    assert r["person"] is None
