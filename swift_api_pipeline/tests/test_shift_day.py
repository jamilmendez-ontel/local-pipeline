"""Tests for the 06:00 PHT shift-day helpers in timer_correction_review.py.

A shift day D is [06:00 Asia/Manila D, 06:00 Asia/Manila D+1), labelled D.
The member-facing Timer Entries email is bucketed on it since 2026-09-28
(before that: the ET calendar date, which is noon PHT to noon PHT).

Run: python -m pytest tests/test_shift_day.py
"""
from datetime import date, datetime, timedelta, timezone
from zoneinfo import ZoneInfo
import os
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.dirname(HERE))

from timer_correction_review import (  # noqa: E402
    TZ_MANILA, SHIFT_DAY_START_HOUR,
    shift_day, shift_day_bounds, form_lookup_bounds, last_closed_shift_day,
)

PHT = ZoneInfo("Asia/Manila")
ET = ZoneInfo("America/New_York")


def pht(y, m, d, hh, mm=0, ss=0):
    return datetime(y, m, d, hh, mm, ss, tzinfo=PHT)


def test_constants():
    assert TZ_MANILA.key == "Asia/Manila"
    assert SHIFT_DAY_START_HOUR == 6


def test_boundary_one_second_before_six_belongs_to_previous_day():
    assert shift_day(pht(2026, 9, 23, 5, 59, 59)) == date(2026, 9, 22)


def test_boundary_exactly_six_belongs_to_new_day():
    assert shift_day(pht(2026, 9, 23, 6, 0, 0)) == date(2026, 9, 23)


def test_evening_shift_start_is_same_day():
    # 18:00 PHT 9/22, the shift start, is shift day 9/22
    assert shift_day(pht(2026, 9, 22, 18, 0)) == date(2026, 9, 22)


def test_overnight_entry_is_previous_day():
    # 02:00 PHT 9/23 is still the shift that started 18:00 PHT 9/22
    assert shift_day(pht(2026, 9, 23, 2, 0)) == date(2026, 9, 22)


def test_overtime_after_six_rolls_to_next_day():
    # 07:00 PHT 9/23 is shift day 9/23 (documented consequence, spec 3.7)
    assert shift_day(pht(2026, 9, 23, 7, 0)) == date(2026, 9, 23)


def test_naive_input_is_utc():
    # 21:59:59 UTC 9/22 == 05:59:59 PHT 9/23
    assert shift_day(datetime(2026, 9, 22, 21, 59, 59)) == date(2026, 9, 22)
    assert shift_day(datetime(2026, 9, 22, 22, 0, 0)) == date(2026, 9, 23)


def test_iso_string_input():
    assert shift_day("2026-09-22T22:00:00+00:00") == date(2026, 9, 23)


def test_et_summer_and_winter_inputs_do_not_move_the_boundary():
    # Asia/Manila has no DST; the boundary is always 06:00 PHT regardless of
    # what ET is doing. 06:00 PHT is 18:00 EDT (summer) and 17:00 EST (winter).
    assert shift_day(datetime(2026, 9, 22, 18, 0, tzinfo=ET)) == date(2026, 9, 23)
    assert shift_day(datetime(2026, 9, 22, 17, 59, tzinfo=ET)) == date(2026, 9, 22)
    assert shift_day(datetime(2026, 1, 14, 17, 0, tzinfo=ET)) == date(2026, 1, 15)
    assert shift_day(datetime(2026, 1, 14, 16, 59, tzinfo=ET)) == date(2026, 1, 14)


def test_bounds_are_half_open_utc_and_24h():
    lo, hi = shift_day_bounds(date(2026, 9, 22))
    assert lo == datetime(2026, 9, 21, 22, 0, tzinfo=timezone.utc)
    assert hi == datetime(2026, 9, 22, 22, 0, tzinfo=timezone.utc)
    assert hi - lo == timedelta(days=1)
    assert lo.tzinfo is not None and hi.tzinfo is not None


def test_bounds_round_trip_through_shift_day():
    for day in (date(2026, 9, 22), date(2026, 1, 14), date(2026, 3, 8), date(2026, 11, 1)):
        lo, hi = shift_day_bounds(day)
        assert shift_day(lo) == day
        assert shift_day(hi - timedelta(seconds=1)) == day
        assert shift_day(hi) == day + timedelta(days=1)


def test_form_lookup_bounds_cover_both_definitions():
    # Old definition: ET calendar day 9/22 = [00:00 EDT 9/22, 00:00 EDT 9/23)
    # New definition: shift day 9/22 = [06:00 PHT 9/22, 06:00 PHT 9/23)
    lo, hi = form_lookup_bounds(date(2026, 9, 22))
    et_lo = datetime(2026, 9, 22, 0, 0, tzinfo=ET)
    et_hi = datetime(2026, 9, 23, 0, 0, tzinfo=ET)
    sd_lo, sd_hi = shift_day_bounds(date(2026, 9, 22))
    assert lo <= et_lo and hi >= et_hi
    assert lo <= sd_lo and hi >= sd_hi
    assert lo == sd_lo                            # opens with the shift day
    assert hi == et_hi.astimezone(timezone.utc)   # closes with the ET day
    assert hi - lo == timedelta(hours=30)


def test_form_lookup_bounds_winter():
    lo, hi = form_lookup_bounds(date(2026, 1, 14))
    assert lo == datetime(2026, 1, 13, 22, 0, tzinfo=timezone.utc)   # 06:00 PHT 1/14
    assert hi == datetime(2026, 1, 15, 5, 0, tzinfo=timezone.utc)    # 00:00 EST 1/15


def test_last_closed_shift_day_at_send_time():
    # The run fires ~06:30 PHT 9/23; the day that just closed is 9/22
    assert last_closed_shift_day(pht(2026, 9, 23, 6, 30)) == date(2026, 9, 22)


def test_last_closed_shift_day_just_before_boundary():
    # If jitter ever fired the run at 05:50 PHT 9/23, 9/22 is NOT closed yet;
    # the last closed day is 9/21. This is why the trigger uses nearMinute(30).
    assert last_closed_shift_day(pht(2026, 9, 23, 5, 50)) == date(2026, 9, 21)


def test_last_closed_shift_day_default_uses_now():
    got = last_closed_shift_day()
    expect = shift_day(datetime.now(timezone.utc)) - timedelta(days=1)
    assert got == expect


# ---------------------------------------------------------------------------
# Wiring: every bucketing site in the emails run uses the shift day.
# retry_db calls the lambda synchronously, so a plain fake with a sync fetch
# that records the SQL and parameters is enough.
# ---------------------------------------------------------------------------

class _RecordingDB:
    def __init__(self):
        self.sql = None
        self.params = None

    def fetch(self, sql, *params):
        self.sql = " ".join(sql.split())
        self.params = params
        return []


def test_entry_date_et_is_gone():
    # The ET-calendar helper must not survive; every caller moved to shift_day.
    import timer_correction_review as tcr
    assert not hasattr(tcr, "_entry_date_et")


def test_get_previous_day_entries_uses_shift_day_bounds():
    """The --send query must be a half-open range on the shift day."""
    import timer_correction_review as tcr
    db = _RecordingDB()
    tcr.get_previous_day_entries(db, target_date=date(2026, 9, 22))
    assert "start_time >= $1 AND start_time < $2" in db.sql
    assert "America/New_York" not in db.sql
    assert db.params == shift_day_bounds(date(2026, 9, 22))


def test_get_previous_day_entries_default_is_last_closed_shift_day():
    import timer_correction_review as tcr
    db = _RecordingDB()
    tcr.get_previous_day_entries(db)
    assert db.params == shift_day_bounds(last_closed_shift_day())


def test_resolve_stale_response_uses_form_lookup_bounds():
    """A member's reply names a date; look it up across BOTH day definitions."""
    import timer_correction_review as tcr
    db = _RecordingDB()
    # Real prefill format: project | site | task | date | duration
    resp = {"entry_id": "deadbeef", "respondent": "a@ontel.co",
            "details": "Proj | Site A | 6. Final COP | Sep 22, 2026 | 1h 30m"}
    tcr._resolve_stale_response(db, resp)
    lo, hi = form_lookup_bounds(date(2026, 9, 22))
    assert db.params[0] == lo
    assert db.params[1] == hi
    assert "start_time >= $1 AND start_time < $2" in db.sql


def test_fetch_classified_day_entries_uses_shift_day_bounds():
    import timer_correction_review as tcr
    db = _RecordingDB()
    tcr._fetch_classified_day_entries(db, "a@ontel.co", date(2026, 9, 22))
    assert "America/New_York" not in db.sql
    assert db.sql.count("start_time >= $2 AND") == 2      # surviving + removals
    assert db.sql.count("start_time < $3") == 2
    lo, hi = shift_day_bounds(date(2026, 9, 22))
    assert db.params == ("a@ontel.co", lo, hi)
