"""The timer pull that runs before the Timer Entries email must reach the shift day it emails.

Background (2026-09-29, run 36492289277): the email moved to ~06:30 PHT on a 06:00 PHT
shift day (2026-09-28 cutover), but the pull in front of it still ended at YESTERDAY in
America/New_York. At 06:25 PHT Tuesday it is 18:25 ET Monday, so the pull stopped at
Sunday (`Date Range: 2026-09-01 to 2026-09-27`) and only the first six hours of the
shift day were in the table: 3 members and 5 entries were emailed where the shift day
had 65 members and 569 entries.

`through_today=True` ends the pull at today ET. Only pipeline-timer-emails.yml uses
it; the ~1:15 AM ET run keeps the closed-day default because Sheena's clean export is
built from it.

On the 1st of the month ET the pull is two month buckets, never one range across the
boundary: staging is replaced per bucket (DELETE WHERE start_date), so a range tagged
with last month's start that also held the 1st would duplicate the 1st's entries once
the new month's bucket is loaded.

Run: python -m pytest tests/test_timer_pull_range.py
"""
from datetime import date, datetime, timedelta
from zoneinfo import ZoneInfo
import os
import re
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
PIPELINE_DIR = os.path.dirname(HERE)
sys.path.insert(0, PIPELINE_DIR)

import extract_timer  # noqa: E402
from extract_timer import calculate_date_range, calculate_date_ranges  # noqa: E402
from timer_correction_review import last_closed_window, window_bounds  # noqa: E402

PHT = ZoneInfo("Asia/Manila")
ET = ZoneInfo("America/New_York")
WORKFLOWS = os.path.join(os.path.dirname(PIPELINE_DIR), ".github", "workflows")


def _et_dates_covered(ranges):
    days = set()
    for start, end in ranges:
        d, last = date.fromisoformat(start), date.fromisoformat(end)
        while d <= last:
            days.add(d)
            d += timedelta(days=1)
    return days


# ---- closed-day default: unchanged --------------------------------------------------

def test_default_mid_month_ends_yesterday_et():
    now = datetime(2026, 9, 29, 1, 15, tzinfo=ET)
    assert calculate_date_ranges(now=now) == [("2026-09-01", "2026-09-28")]


def test_default_on_the_first_is_the_previous_month_only():
    now = datetime(2026, 10, 1, 1, 15, tzinfo=ET)
    assert calculate_date_ranges(now=now) == [("2026-09-01", "2026-09-30")]


def test_calculate_date_range_keeps_its_old_contract(monkeypatch):
    class _Clock(datetime):
        @classmethod
        def now(cls, tz=None):
            return datetime(2026, 9, 29, 1, 15, tzinfo=ET).astimezone(tz)

    monkeypatch.setattr(extract_timer, "datetime", _Clock)
    assert calculate_date_range() == ("2026-09-01", "2026-09-28")


# ---- through today: what the email run needs ----------------------------------------

def test_through_today_is_the_run_that_failed():
    """06:25 PHT Tue 2026-09-29 = 18:25 ET Mon 2026-09-28. The old pull ended 09-27."""
    now = datetime(2026, 9, 29, 6, 25, tzinfo=PHT)
    assert calculate_date_ranges(now=now) == [("2026-09-01", "2026-09-27")]
    assert calculate_date_ranges(now=now, through_today=True) == [("2026-09-01", "2026-09-28")]


def test_through_today_covers_the_window_being_emailed_every_day_of_a_year():
    """For every 18:30 ET and 09:00 ET send, the pull holds the ET date of the closed window."""
    for hour, minute in ((18, 30), (9, 0)):
        send = datetime(2026, 1, 1, hour, minute, tzinfo=ET)
        for _ in range(366):
            day, part = last_closed_window(send)
            lo, hi = window_bounds(day, part)
            needed = {lo.astimezone(ET).date(), (hi - timedelta(seconds=1)).astimezone(ET).date()}
            covered = _et_dates_covered(calculate_date_ranges(now=send, through_today=True))
            assert needed <= covered, f"send {send:%Y-%m-%d %H:%M}: pull misses {sorted(needed - covered)}"
            send += timedelta(days=1)


def test_closed_day_default_does_not_cover_it():
    """The regression itself: without through_today the 18:30 ET pull is a day short."""
    send = datetime(2026, 9, 29, 18, 30, tzinfo=ET)
    day, part = last_closed_window(send)
    lo, hi = window_bounds(day, part)
    needed = {lo.astimezone(ET).date(), (hi - timedelta(seconds=1)).astimezone(ET).date()}
    assert not needed <= _et_dates_covered(calculate_date_ranges(now=send))


def test_through_today_on_the_first_is_two_month_buckets():
    """18:30 ET Thu 2026-10-01 (first window of Oct 1): the pull is Sep as one bucket, Oct 1 alone.
    The 09:00 ET run on the 1st needs the previous day's second window, i.e. the September bucket."""
    now = datetime(2026, 10, 2, 6, 30, tzinfo=PHT)
    assert calculate_date_ranges(now=now, through_today=True) == [
        ("2026-09-01", "2026-09-30"),
        ("2026-10-01", "2026-10-01"),
    ]


def test_buckets_never_cross_a_month_and_never_overlap():
    now = datetime(2026, 1, 1, 18, 30, tzinfo=ET)
    for _ in range(366):
        for through_today in (False, True):
            ranges = calculate_date_ranges(now=now, through_today=through_today)
            seen = set()
            for start, end in ranges:
                s, e = date.fromisoformat(start), date.fromisoformat(end)
                assert s.day == 1 and s <= e and (s.year, s.month) == (e.year, e.month)
                days = _et_dates_covered([(start, end)])
                assert not days & seen
                seen |= days
        now += timedelta(days=1)


def test_january_first_reaches_back_into_december():
    now = datetime(2027, 1, 1, 18, 30, tzinfo=ET)
    assert calculate_date_ranges(now=now, through_today=True) == [
        ("2026-12-01", "2026-12-31"),
        ("2027-01-01", "2027-01-01"),
    ]


# ---- main.py runs every bucket -------------------------------------------------------

def test_timer_pipeline_extracts_and_transforms_each_bucket(monkeypatch):
    import main
    import transform

    calls = []
    monkeypatch.setattr(extract_timer, "calculate_date_ranges",
                        lambda through_today=False: [("2026-09-01", "2026-09-30"),
                                                     ("2026-10-01", "2026-10-01")])

    def fake_extract(start_date=None, end_date=None, **kw):
        calls.append(("extract", start_date, end_date))
        return f"run-{start_date}"

    def fake_transform(run_id):
        calls.append(("transform", run_id))
        return []

    monkeypatch.setattr(extract_timer, "run_timer_pipeline", fake_extract)
    monkeypatch.setattr(transform, "run_timer_transform", fake_transform)

    assert main.run_timer_pipeline_full(through_today=True) is True
    assert calls == [
        ("extract", "2026-09-01", "2026-09-30"), ("transform", "run-2026-09-01"),
        ("extract", "2026-10-01", "2026-10-01"), ("transform", "run-2026-10-01"),
    ]


def test_timer_pipeline_passes_the_flag_down(monkeypatch):
    import main
    import transform

    seen = []
    monkeypatch.setattr(extract_timer, "calculate_date_ranges",
                        lambda through_today=False: seen.append(through_today)
                        or [("2026-09-01", "2026-09-28")])
    monkeypatch.setattr(extract_timer, "run_timer_pipeline", lambda **kw: "run-1")
    monkeypatch.setattr(transform, "run_timer_transform", lambda run_id: [])

    main.run_timer_pipeline_full()
    main.run_timer_pipeline_full(through_today=True)
    assert seen == [False, True]


def test_an_absent_project_in_any_bucket_is_still_reported(monkeypatch):
    import main
    import transform

    monkeypatch.setattr(extract_timer, "calculate_date_ranges",
                        lambda through_today=False: [("2026-09-01", "2026-09-30"),
                                                     ("2026-10-01", "2026-10-01")])
    monkeypatch.setattr(extract_timer, "run_timer_pipeline",
                        lambda start_date=None, end_date=None, **kw: f"run-{start_date}")
    carried = {"run-2026-09-01": [{"project": "TECH-OPS: TS19", "project_did": "x", "rows": 40,
                                   "project_absent": True}],
               "run-2026-10-01": []}
    monkeypatch.setattr(transform, "run_timer_transform", lambda run_id: carried[run_id])

    outcome = main.run_timer_pipeline_full(through_today=True)
    assert outcome.abnormal_projects == ["TECH-OPS: TS19"]
    assert "40" in outcome.detail


def test_a_bucket_with_no_entries_does_not_stop_the_run(monkeypatch):
    """The 1st alone can be empty (a weekend or holiday 1st, nobody has clocked in by the
    pull). transform_timer_activities used to return a bare 0 there and
    run_timer_transform unpacked it as a pair: TypeError, and no email that day."""
    import transform

    class _EmptyRunDb:
        def fetchrow(self, *a):
            return None

        def fetch(self, *a):
            raise AssertionError("an empty run must not read or write staging")

        execute = executemany = fetch

    monkeypatch.setattr(transform, "get_db", lambda: _EmptyRunDb())
    monkeypatch.setattr(transform, "validate_transform_counts", lambda *a, **kw: None)
    assert transform.run_timer_transform("33333333-3333-3333-3333-333333333333") == []


# ---- the workflows ---------------------------------------------------------------------

def _timer_commands(workflow):
    text = open(os.path.join(WORKFLOWS, workflow), encoding="utf-8").read()
    return re.findall(r"^\s*python -u main\.py --pipeline timer\b.*$", text, flags=re.M)


def test_email_workflow_pulls_through_today_on_the_run_and_the_retry():
    cmds = _timer_commands("pipeline-timer-emails.yml")
    assert len(cmds) == 2
    assert all("--through-today" in c for c in cmds), cmds


def test_nightly_workflow_keeps_the_closed_day_pull():
    cmds = _timer_commands("pipeline-timer.yml")
    assert len(cmds) == 2
    assert not any("--through-today" in c for c in cmds), cmds
