"""Tests for the two-window (Eastern day) helpers in timer_correction_review.py.

Window = (Eastern date D, part). first = [00:00 ET D, 18:00 ET D),
second = [18:00 ET D, 00:00 ET D+1). Boundaries follow US DST on purpose.

Run: python -m pytest tests/test_email_windows.py -q
"""
from datetime import date, datetime, timedelta, timezone
from zoneinfo import ZoneInfo
import os
import sys

import pytest

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.dirname(HERE))

import timer_correction_review as tcr  # noqa: E402
from timer_correction_review import (  # noqa: E402
    WINDOW_SPLIT_HOUR_ET, WINDOW_PARTS, LEGACY_WINDOW,
    window_bounds, window_of, last_closed_window, legacy_full_bounds,
    form_lookup_bounds, window_label, email_subject, coverage_note_html,
)

ET = ZoneInfo("America/New_York")
PHT = ZoneInfo("Asia/Manila")


def et(y, m, d, hh, mm=0, ss=0):
    return datetime(y, m, d, hh, mm, ss, tzinfo=ET)


def test_constants():
    assert WINDOW_SPLIT_HOUR_ET == 18
    assert WINDOW_PARTS == ("first", "second")
    assert LEGACY_WINDOW == "full"


def test_shift_day_helpers_are_gone():
    for name in ("shift_day", "shift_day_bounds", "last_closed_shift_day", "SHIFT_DAY_START_HOUR"):
        assert not hasattr(tcr, name), name


# ---- bounds -----------------------------------------------------------------

def test_window_bounds_summer_first():
    lo, hi = window_bounds(date(2026, 10, 2), "first")
    assert lo == et(2026, 10, 2, 0).astimezone(timezone.utc)
    assert hi == et(2026, 10, 2, 18).astimezone(timezone.utc)
    assert lo.tzinfo == timezone.utc and hi.tzinfo == timezone.utc
    assert hi - lo == timedelta(hours=18)
    assert lo == datetime(2026, 10, 2, 4, 0, tzinfo=timezone.utc)   # EDT = UTC-4


def test_window_bounds_summer_second():
    lo, hi = window_bounds(date(2026, 10, 2), "second")
    assert lo == et(2026, 10, 2, 18).astimezone(timezone.utc)
    assert hi == et(2026, 10, 3, 0).astimezone(timezone.utc)
    assert hi - lo == timedelta(hours=6)


def test_window_bounds_winter_uses_est():
    lo, hi = window_bounds(date(2026, 12, 10), "first")
    assert lo == datetime(2026, 12, 10, 5, 0, tzinfo=timezone.utc)   # EST = UTC-5
    assert hi == datetime(2026, 12, 10, 23, 0, tzinfo=timezone.utc)


def test_window_bounds_dst_transition_days():
    # Fall back 2026-11-01: the first window is 19 real hours, still ends 18:00 local.
    lo, hi = window_bounds(date(2026, 11, 1), "first")
    assert hi.astimezone(ET).hour == 18 and hi - lo == timedelta(hours=19)
    lo2, hi2 = window_bounds(date(2026, 11, 1), "second")
    assert lo2 == hi and hi2.astimezone(ET) == et(2026, 11, 2, 0)
    # Spring forward 2026-03-08: 17 real hours, still ends 18:00 local.
    lo, hi = window_bounds(date(2026, 3, 8), "first")
    assert hi.astimezone(ET).hour == 18 and hi - lo == timedelta(hours=17)


def test_window_bounds_rejects_unknown_part():
    with pytest.raises(ValueError):
        window_bounds(date(2026, 10, 2), "full")


def test_windows_tile_the_day():
    d = date(2026, 10, 2)
    _, hi_first = window_bounds(d, "first")
    lo_second, hi_second = window_bounds(d, "second")
    lo_next, _ = window_bounds(d + timedelta(days=1), "first")
    assert hi_first == lo_second and hi_second == lo_next


# ---- window_of --------------------------------------------------------------

def test_window_of_before_and_at_split():
    assert window_of(et(2026, 10, 2, 17, 59, 59)) == (date(2026, 10, 2), "first")
    assert window_of(et(2026, 10, 2, 18, 0, 0)) == (date(2026, 10, 2), "second")


def test_window_of_crosses_utc_midnight_not_et_midnight():
    # 01:30 UTC Oct 3 is 21:30 EDT Oct 2 -> still Oct 2, second.
    assert window_of(datetime(2026, 10, 3, 1, 30, tzinfo=timezone.utc)) == (date(2026, 10, 2), "second")


def test_window_of_naive_is_utc_and_iso_string():
    assert window_of(datetime(2026, 10, 2, 4, 0)) == (date(2026, 10, 2), "first")
    assert window_of("2026-10-02T03:59:59+00:00") == (date(2026, 10, 1), "second")


# ---- last_closed_window -----------------------------------------------------

def test_last_closed_window_at_first_send():
    assert last_closed_window(et(2026, 10, 2, 18, 30)) == (date(2026, 10, 2), "first")


def test_last_closed_window_at_second_send():
    assert last_closed_window(et(2026, 10, 3, 9, 0)) == (date(2026, 10, 2), "second")


def test_last_closed_window_late_first_send_winter():
    assert last_closed_window(et(2026, 12, 10, 19, 30)) == (date(2026, 12, 10), "first")


def test_last_closed_window_before_first_close():
    # 5:30 PM ET: the first window is still open; the last closed one is yesterday's second.
    assert last_closed_window(et(2026, 12, 10, 17, 30)) == (date(2026, 12, 9), "second")


def test_last_closed_window_exactly_at_close():
    assert last_closed_window(et(2026, 10, 2, 18, 0, 0)) == (date(2026, 10, 2), "first")
    assert last_closed_window(et(2026, 10, 3, 0, 0, 0)) == (date(2026, 10, 2), "second")


def test_last_closed_window_default_uses_now():
    assert last_closed_window() == last_closed_window(datetime.now(timezone.utc))


# ---- legacy + form lookup ---------------------------------------------------

def test_legacy_full_bounds_is_the_old_shift_day():
    lo, hi = legacy_full_bounds(date(2026, 9, 30))
    assert lo == datetime(2026, 9, 30, 6, tzinfo=PHT).astimezone(timezone.utc)
    assert hi - lo == timedelta(days=1)


def test_form_lookup_bounds_union():
    d = date(2026, 10, 2)
    lo, hi = form_lookup_bounds(d)
    lo_first, _ = window_bounds(d, "first")
    _, hi_second = window_bounds(d, "second")
    lo_legacy, hi_legacy = legacy_full_bounds(d)
    assert lo == min(lo_first, lo_legacy)
    assert hi == max(hi_second, hi_legacy)
    # Summer: legacy opens 18:00 ET the day before; closes 18:00 ET D. Span is 30 h.
    assert lo == et(2026, 10, 1, 18).astimezone(timezone.utc)
    assert hi == et(2026, 10, 3, 0).astimezone(timezone.utc)


def test_form_lookup_bounds_winter_covers_legacy_tail():
    d = date(2026, 12, 10)
    lo, hi = form_lookup_bounds(d)
    # 06:00 PHT Dec 10 = 22:00 UTC Dec 9 = 17:00 EST Dec 9: earlier than 00:00 EST Dec 10.
    assert lo == datetime(2026, 12, 9, 22, tzinfo=timezone.utc)
    assert hi == et(2026, 12, 11, 0).astimezone(timezone.utc)


# ---- copy -------------------------------------------------------------------

def test_window_label_and_subject():
    assert window_label("first") == "12 AM to 6 PM ET"
    assert window_label("second") == "6 PM to 12 AM ET"
    assert email_subject(date(2026, 10, 2), "first") == "Timer Activity Entries - October 02, 2026 (12 AM to 6 PM ET)"
    assert email_subject(date(2026, 10, 2), "second") == "Timer Activity Entries - October 02, 2026 (6 PM to 12 AM ET)"


def test_coverage_note_first_summer():
    html = coverage_note_html(date(2026, 10, 2), "first")
    assert "What this email covers" in html
    assert "Friday, October 2" in html and "(Eastern date)" in html
    assert "12:00 AM to 6:00 PM Eastern" in html
    assert "12:00 PM Oct 02 to 6:00 AM Oct 03 Philippine time" in html
    assert "sent at 9 AM Eastern / 9 PM Philippine time" in html
    assert "—" not in html


def test_coverage_note_second_summer():
    html = coverage_note_html(date(2026, 10, 2), "second")
    assert "6:00 PM to 12:00 AM Eastern" in html
    assert "6:00 AM Oct 03 to 12:00 PM Oct 03 Philippine time" in html
    assert "were in the earlier email" in html


def test_coverage_note_winter_pht_readings_shift_one_hour():
    html = coverage_note_html(date(2026, 12, 10), "first")
    assert "1:00 PM Dec 10 to 7:00 AM Dec 11 Philippine time" in html
