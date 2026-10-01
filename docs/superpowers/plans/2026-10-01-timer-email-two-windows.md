# Timer Entries email: two windows per Eastern day — Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Replace the single 06:00-PHT shift-day Timer Entries email with two emails per Eastern calendar day (12 AM to 6 PM ET sent 6:30 PM ET; 6 PM to 12 AM ET sent 9 AM ET next day), keyed and labelled by the Eastern date, with a callout that states the covered window in both time zones.

**Architecture:** A window is `(day: date, part: 'first'|'second')`. One helper block in `timer_correction_review.py` owns the bounds, the clock rule and the copy; every bucketing call site (send, reply lookup, confirmation, reminders, resend) takes a window instead of a shift day. `app_timer.daily_notifications` gains a `window` column and a three-column unique key (migration 275). The GitHub workflow takes optional `date`/`window` inputs for backfills. Apps Script fires the same dispatch twice a day on the Eastern clock.

**Tech Stack:** Python 3.12, asyncpg via the pipeline's sync bridge (`retry_db`, `get_db`), pytest, Postgres (Supabase OntelDB), GitHub Actions, Google Apps Script.

**Spec:** `docs/superpowers/specs/2026-10-01-timer-email-two-windows-design.md`

## Global Constraints

- Boundaries are `America/New_York` wall-clock 00:00 / 18:00 / 24:00 and follow US DST (spec 1).
- Part names are exactly `first` and `second`; legacy rows are `full` (spec 4).
- Subject format: `Timer Activity Entries - {Month D, YYYY} (12 AM to 6 PM ET)` or `(6 PM to 12 AM ET)` (spec 5).
- Only members with at least one entry in the window are emailed (spec 5).
- `--date` and `--window` must be given together or not at all (spec 3).
- No em dashes in new user-facing copy (project rule); `&mdash;` already present in old HTML may stay.
- Migration file numbering: next free is `275` (273 and 274 exist on main). Rollback file alongside.
- Tests run from `swift_api_pipeline/` with `python -m pytest tests/<file> -q` using the local venv `venv\Scripts\python.exe`.

## Review Focus

1. A 5:30 PM ET run (winter, trigger still pinned to PHT) must pick `(yesterday, second)`, never `(today, first)` with an open window — Task 1 test `test_last_closed_window_before_first_close`.
2. The DST transition days: `window_bounds` on 2026-11-01 (fall back) and 2026-03-08 (spring forward) must still give a `first` window ending at 18:00 local and a `second` window ending at 00:00 local next day — Task 1 test `test_window_bounds_dst_transition_days`.
3. `--date` without `--window` must exit with a parser error, not silently default — Task 3 test `test_cli_rejects_date_without_window`.
4. Resend must not pair a `full` legacy row with first/second bounds, or the 7-day lookback would re-send whole days as "new" — Task 5 test `test_find_days_needing_resend_uses_row_window`.
5. A reply naming the label date must still resolve entries from either new window and from a legacy shift-day email — Task 1 test `test_form_lookup_bounds_union`.

---

### Task 1: Window helpers, copy helpers, tests

**Files:**
- Modify: `swift_api_pipeline/timer_correction_review.py:55-125` (replace the shift-day helper block)
- Create: `swift_api_pipeline/tests/test_email_windows.py`
- Delete: `swift_api_pipeline/tests/test_shift_day.py`

**Interfaces:**
- Produces:
  - `WINDOW_SPLIT_HOUR_ET: int = 18`, `WINDOW_PARTS: tuple = ("first", "second")`, `LEGACY_WINDOW = "full"`
  - `window_bounds(day: date, part: str) -> tuple[datetime, datetime]` (UTC, half-open)
  - `window_of(dt) -> tuple[date, str]`
  - `last_closed_window(now: datetime | None = None) -> tuple[date, str]`
  - `legacy_full_bounds(day: date) -> tuple[datetime, datetime]` (the old 06:00 PHT shift day)
  - `form_lookup_bounds(day: date) -> tuple[datetime, datetime]`
  - `window_label(part: str) -> str` returns `"12 AM to 6 PM ET"` / `"6 PM to 12 AM ET"`
  - `email_subject(day: date, part: str) -> str`
  - `coverage_note_html(day: date, part: str) -> str`
  - `TZ_MANILA` stays.
- Removed: `SHIFT_DAY_START_HOUR`, `shift_day`, `shift_day_bounds`, `last_closed_shift_day`.

- [ ] **Step 1: Write the failing tests**

Create `swift_api_pipeline/tests/test_email_windows.py`:

```python
"""Tests for the two-window (Eastern day) helpers in timer_correction_review.py.

Window = (Eastern date D, part). first = [00:00 ET D, 18:00 ET D),
second = [18:00 ET D, 00:00 ET D+1). Boundaries follow US DST on purpose.

Run: python -m pytest tests/test_email_windows.py -q
"""
from datetime import date, datetime, timedelta, timezone
from zoneinfo import ZoneInfo
import os
import sys

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
    import pytest
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
    got = last_closed_window()
    assert got in {last_closed_window(datetime.now(timezone.utc) - timedelta(seconds=s)) for s in (0, 1)}


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
    assert lo == datetime(2026, 12, 9, 22, tzinfo=timezone.utc)      # 06:00 PHT Dec 10 = 22:00 UTC Dec 9 = 17:00 EST Dec 9
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
```

- [ ] **Step 2: Run the tests to verify they fail**

Run (from `swift_api_pipeline/`): `venv\Scripts\python.exe -m pytest tests/test_email_windows.py -q`
Expected: ImportError on `WINDOW_SPLIT_HOUR_ET` (collection error).

- [ ] **Step 3: Replace the helper block**

In `timer_correction_review.py`, replace everything from the comment `# The member-facing Timer Entries email is bucketed on a SHIFT DAY` (line 57) through the end of `last_closed_shift_day` (line 119) with:

```python
# The member-facing Timer Entries email is bucketed on an EASTERN DAY split
# into two WINDOWS (decided 2026-10-01, replacing the 06:00 PHT shift day of
# 2026-09-28):
#   first  = [00:00 ET D, 18:00 ET D)   sent ~18:30 ET on D
#   second = [18:00 ET D, 00:00 ET D+1) sent ~09:00 ET on D+1
# Both are labelled with the Eastern date D, the same day Swift and DRMC
# use. Boundaries follow US DST on purpose (Jamil's call), so in PHT terms
# the split sits at 06:00 in summer and 07:00 in winter. Everything the
# emails run touches (--send, --remind, --resend, the change records
# --apply writes) uses these helpers; nothing downstream (DRMC, variance,
# exports) does.
WINDOW_SPLIT_HOUR_ET = 18
WINDOW_PARTS = ("first", "second")
LEGACY_WINDOW = "full"          # daily_notifications rows written before this change
_LEGACY_SHIFT_START_HOUR_PHT = 6


def _as_aware_utc(dt) -> datetime:
    """ISO string or datetime -> tz-aware datetime; naive means UTC."""
    if isinstance(dt, str):
        dt = datetime.fromisoformat(dt)
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt


def _et_wall(day: date, hour: int) -> datetime:
    """`hour` o'clock Eastern on `day` (hour 24 = midnight ending the day), as UTC."""
    base = datetime(day.year, day.month, day.day, tzinfo=TZ_EASTERN)
    if hour == 24:
        base = datetime.combine(day + timedelta(days=1), datetime.min.time(), tzinfo=TZ_EASTERN)
        return base.astimezone(timezone.utc)
    return base.replace(hour=hour).astimezone(timezone.utc)


def window_bounds(day: date, part: str) -> tuple[datetime, datetime]:
    """[lo, hi) in UTC for the window (`day`, `part`). Use as
    `start_time >= $lo AND start_time < $hi` (sargable on the index)."""
    if part == "first":
        return _et_wall(day, 0), _et_wall(day, WINDOW_SPLIT_HOUR_ET)
    if part == "second":
        return _et_wall(day, WINDOW_SPLIT_HOUR_ET), _et_wall(day, 24)
    raise ValueError(f"unknown window part {part!r}; expected one of {WINDOW_PARTS}")


def window_of(dt) -> tuple[date, str]:
    """(Eastern date, part) the instant belongs to."""
    local = _as_aware_utc(dt).astimezone(TZ_EASTERN)
    part = "first" if local.hour < WINDOW_SPLIT_HOUR_ET else "second"
    return local.date(), part


def last_closed_window(now: datetime | None = None) -> tuple[date, str]:
    """The most recent window whose end is at or before `now`.

    ~18:30 ET on D -> (D, first); ~09:00 ET on D+1 -> (D, second). A run
    that fires before 18:00 ET correctly returns the previous day's second
    window rather than emailing a still-open one.
    """
    now = _as_aware_utc(now or datetime.now(timezone.utc))
    day, part = window_of(now)
    if part == "second":
        return day, "first"
    return day - timedelta(days=1), "second"


def legacy_full_bounds(day: date) -> tuple[datetime, datetime]:
    """The pre-2026-10-01 shift day: [06:00 PHT day, 06:00 PHT day+1) in UTC.
    Only for daily_notifications rows with window = 'full'."""
    lo = datetime(day.year, day.month, day.day, _LEGACY_SHIFT_START_HOUR_PHT,
                  tzinfo=TZ_MANILA).astimezone(timezone.utc)
    return lo, lo + timedelta(days=1)


def form_lookup_bounds(day: date) -> tuple[datetime, datetime]:
    """Window for resolving a member's form reply that names `day`.

    Union of both new windows of `day` and the legacy shift day labelled
    `day`, so replies to emails sent under either definition resolve. The
    group lookup also matches on site/task/start, so the extra hours cannot
    make it ambiguous. Safe to keep forever.
    """
    lo_first, _ = window_bounds(day, "first")
    _, hi_second = window_bounds(day, "second")
    lo_legacy, hi_legacy = legacy_full_bounds(day)
    return min(lo_first, lo_legacy), max(hi_second, hi_legacy)


def window_label(part: str) -> str:
    return "12 AM to 6 PM ET" if part == "first" else "6 PM to 12 AM ET"


def email_subject(day: date, part: str) -> str:
    return f"Timer Activity Entries - {day.strftime('%B %d, %Y')} ({window_label(part)})"


def _clock(dt: datetime) -> str:
    """'6:00 AM Sep 28' (no leading zero on the hour, portable)."""
    return f"{dt.strftime('%I').lstrip('0')}:{dt.strftime('%M %p %b %d')}"


def _clock_only(dt: datetime) -> str:
    return f"{dt.strftime('%I').lstrip('0')}:{dt.strftime('%M %p')}"


def coverage_note_html(day: date, part: str) -> str:
    """The 'What this email covers' callout. States the window in both
    zones, derived from the bounds so DST is automatic, and names the
    other email of the same Eastern day."""
    lo, hi = window_bounds(day, part)
    lo_et, hi_et = lo.astimezone(TZ_EASTERN), hi.astimezone(TZ_EASTERN)
    lo_pht, hi_pht = lo.astimezone(TZ_MANILA), hi.astimezone(TZ_MANILA)
    label = f"{day.strftime('%A, %B')} {day.day}"
    if part == "first":
        tail = ("Entries started after 6:00 PM Eastern will be in the evening email, "
                "sent at 9 AM Eastern / 9 PM Philippine time.")
    else:
        tail = "Your entries from 12 AM to 6 PM Eastern were in the earlier email."
    return f"""
                <div style="background:#fff8e1;border-left:4px solid #f9a825;border-radius:4px;padding:10px 14px;margin:0 0 16px;font-size:13px;color:#5d4037;">
                    <strong>What this email covers:</strong> <strong>{label}</strong> (Eastern date),
                    <strong>{_clock_only(lo_et)} to {_clock_only(hi_et)} Eastern</strong>,
                    which is {_clock(lo_pht)} to {_clock(hi_pht)} Philippine time.
                    {tail}
                </div>
    """
```

Also delete the existing `_as_aware_utc` if it would now be duplicated (keep exactly one definition), and delete `tests/test_shift_day.py`.

- [ ] **Step 4: Run the new tests**

Run: `venv\Scripts\python.exe -m pytest tests/test_email_windows.py -q`
Expected: all pass. (`import timer_correction_review` still works because the removed names are not referenced at import time; the call sites that reference them are fixed in Tasks 3 and 5, and `python -c "import timer_correction_review"` must succeed now.)

- [ ] **Step 5: Commit**

```bash
git add swift_api_pipeline/timer_correction_review.py swift_api_pipeline/tests/test_email_windows.py
git rm -q swift_api_pipeline/tests/test_shift_day.py
git commit -m "feat(timer emails): Eastern-day two-window helpers, subject and coverage note"
```

---

### Task 2: Migration 275 and rollback

**Files:**
- Create: `swift_api_pipeline/migrations/275_timer_notifications_window.sql`
- Create: `swift_api_pipeline/migrations/275_timer_notifications_window_ROLLBACK.sql`

**Interfaces:**
- Produces: column `app_timer.daily_notifications.window text not null default 'full'`, constraint `daily_notifications_user_email_send_date_window_key`.

- [ ] **Step 1: Write the migration**

```sql
-- ═══════════════════════════════════════════════════════════════════════════════
-- 275_timer_notifications_window.sql
-- Timer Entries email: two windows per Eastern day (first = 12 AM to 6 PM ET,
-- second = 6 PM to 12 AM ET). One notification row per (member, Eastern date,
-- window). Rows written before this change covered a whole day (ET day before
-- 2026-09-28, 06:00 PHT shift day after) and are marked 'full'.
--
-- Spec: docs/superpowers/specs/2026-10-01-timer-email-two-windows-design.md
-- Preflight: pg_depend shows no view/MV/function depending on this table
-- (migration 117 notes; re-checked before apply). The only writer is
-- timer_correction_review.py; its ON CONFLICT target changes in the same PR,
-- so apply this immediately after that PR is merged, never before.
-- ═══════════════════════════════════════════════════════════════════════════════
BEGIN;

ALTER TABLE app_timer.daily_notifications
    ADD COLUMN IF NOT EXISTS window text NOT NULL DEFAULT 'full';

ALTER TABLE app_timer.daily_notifications
    DROP CONSTRAINT IF EXISTS daily_notifications_window_check;
ALTER TABLE app_timer.daily_notifications
    ADD CONSTRAINT daily_notifications_window_check
    CHECK (window IN ('full', 'first', 'second'));

-- The old key survived the 117 rename under its original stg_ name.
ALTER TABLE app_timer.daily_notifications
    DROP CONSTRAINT IF EXISTS stg_timer_daily_notifications_user_email_send_date_key;
ALTER TABLE app_timer.daily_notifications
    DROP CONSTRAINT IF EXISTS daily_notifications_user_email_send_date_key;

ALTER TABLE app_timer.daily_notifications
    ADD CONSTRAINT daily_notifications_user_email_send_date_window_key
    UNIQUE (user_email, send_date, window);

COMMENT ON COLUMN app_timer.daily_notifications.window IS
    'Email window: first = 12 AM to 6 PM ET, second = 6 PM to 12 AM ET (since 2026-10-01); full = whole-day sends before that.';

COMMIT;
```

Before applying, run on the live DB and paste the result into the file header as a comment:

```sql
SELECT conname FROM pg_constraint
WHERE conrelid = 'app_timer.daily_notifications'::regclass;
SELECT dependent.relname, dependent.relkind
FROM pg_depend d
JOIN pg_rewrite r ON r.oid = d.objid
JOIN pg_class dependent ON dependent.oid = r.ev_class
WHERE d.refobjid = 'app_timer.daily_notifications'::regclass;
```

Expected: one unique constraint whose name matches one of the two `DROP CONSTRAINT IF EXISTS` lines (adjust the file if the live name differs), and zero dependent relations.

- [ ] **Step 2: Write the rollback**

```sql
-- 275_timer_notifications_window_ROLLBACK.sql
-- Valid only while no (user_email, send_date) has more than one row.
BEGIN;
ALTER TABLE app_timer.daily_notifications
    DROP CONSTRAINT IF EXISTS daily_notifications_user_email_send_date_window_key;
ALTER TABLE app_timer.daily_notifications
    DROP CONSTRAINT IF EXISTS daily_notifications_window_check;
ALTER TABLE app_timer.daily_notifications
    ADD CONSTRAINT daily_notifications_user_email_send_date_key UNIQUE (user_email, send_date);
ALTER TABLE app_timer.daily_notifications DROP COLUMN IF EXISTS window;
COMMIT;
```

- [ ] **Step 3: Commit**

```bash
git add swift_api_pipeline/migrations/275_timer_notifications_window.sql swift_api_pipeline/migrations/275_timer_notifications_window_ROLLBACK.sql
git commit -m "mig 275: daily_notifications.window + (user_email, send_date, window) key"
```

---

### Task 3: Send path on windows, CLI `--window`

**Files:**
- Modify: `swift_api_pipeline/timer_correction_review.py` — `get_previous_day_entries` (~line 712), `send_daily_emails` (~1050, the subject at ~1181, the insert at ~1213), `run_send` (~1352), `main` (~3565)
- Test: `swift_api_pipeline/tests/test_email_windows.py` (append)

**Interfaces:**
- Consumes: Task 1 helpers.
- Produces:
  - `get_previous_day_entries(db, target_date=None, part=None) -> list[dict]`
  - `send_daily_emails(db, entries, test_mode=False, target_date=None, part=None)`
  - `run_send(test_mode=False, target_date=None, part=None)`
  - CLI: `--window {first,second}`; `--date` help text updated.

- [ ] **Step 1: Write the failing tests** (append to `tests/test_email_windows.py`)

```python
# ---- send path wiring -------------------------------------------------------

class _RecordingDB:
    def __init__(self):
        self.calls = []          # (sql, params)

    def fetch(self, sql, *params):
        self.calls.append((" ".join(sql.split()), params))
        return []

    def fetchrow(self, sql, *params):
        self.calls.append((" ".join(sql.split()), params))
        return None

    def execute(self, sql, *params):
        self.calls.append((" ".join(sql.split()), params))
        return "OK"

    @property
    def sql(self):
        return self.calls[-1][0]

    @property
    def params(self):
        return self.calls[-1][1]


def test_get_previous_day_entries_uses_window_bounds():
    db = _RecordingDB()
    tcr.get_previous_day_entries(db, target_date=date(2026, 9, 30), part="second")
    assert "start_time >= $1 AND start_time < $2" in db.sql
    assert db.params == window_bounds(date(2026, 9, 30), "second")


def test_get_previous_day_entries_default_is_last_closed_window():
    db = _RecordingDB()
    tcr.get_previous_day_entries(db)
    day, part = last_closed_window()
    assert db.params == window_bounds(day, part)


def test_get_previous_day_entries_requires_both_or_neither():
    import pytest
    db = _RecordingDB()
    with pytest.raises(ValueError):
        tcr.get_previous_day_entries(db, target_date=date(2026, 9, 30))
    with pytest.raises(ValueError):
        tcr.get_previous_day_entries(db, part="first")


class _FakeGmail:
    """Just enough of the Gmail API surface for send_daily_emails."""
    def __init__(self):
        self.sent = []

    def users(self):
        return self

    def messages(self):
        return self

    def send(self, userId, body):
        self.sent.append(body)
        return self

    def get(self, userId, id, format, metadataHeaders):
        return self

    def execute(self):
        if self.sent and "raw" in self.sent[-1] and not self.sent[-1].get("_executed"):
            self.sent[-1]["_executed"] = True
            return {"threadId": "thr1", "id": "msg1"}
        return {"payload": {"headers": [{"name": "Message-ID", "value": "<m1@x>"}]}}


def _decode_subject(raw_body):
    import base64
    import email
    msg = email.message_from_bytes(base64.urlsafe_b64decode(raw_body["raw"]))
    return msg["Subject"], msg.get_payload()[0].get_payload(decode=True).decode()


def test_send_daily_emails_subject_callout_and_window_key(monkeypatch):
    import gmail_client
    fake = _FakeGmail()
    monkeypatch.setattr(gmail_client, "authenticate", lambda: fake)
    monkeypatch.setattr(gmail_client, "masked_sender", lambda service, name: f"{name} <x@ontel.co>")
    db = _RecordingDB()
    lo, _ = window_bounds(date(2026, 9, 30), "second")
    entries = [{
        "project_did": "p1", "project": "Proj", "user_email": "a@ontel.co",
        "start_time": lo + timedelta(minutes=5), "end_time": lo + timedelta(minutes=65),
        "duration_min": 60, "site_name": "Site A", "site_id": "S1",
        "task": "6. Final COP", "task_clean": "Final COP", "asset_did": "ad1",
    }]
    tcr.send_daily_emails(db, entries, test_mode=True, target_date=date(2026, 9, 30), part="second")
    assert len(fake.sent) == 1
    subject, html = _decode_subject(fake.sent[0])
    assert subject == "Timer Activity Entries - September 30, 2026 (6 PM to 12 AM ET)"
    assert "What this email covers" in html and "6:00 PM to 12:00 AM Eastern" in html
    inserts = [c for c in db.calls if "INSERT INTO" in c[0] and "daily_notifications" in c[0]]
    assert len(inserts) == 1
    sql, params = inserts[0]
    assert "(user_email, send_date, window, thread_id, message_id," in sql
    assert "ON CONFLICT (user_email, send_date, window)" in sql
    assert params[0] == "a@ontel.co" and params[1] == date(2026, 9, 30) and params[2] == "second"


def test_cli_rejects_date_without_window(monkeypatch):
    import pytest
    monkeypatch.setattr(sys, "argv", ["x", "--send", "--date", "2026-09-30"])
    monkeypatch.setattr(tcr, "check_token_health", lambda: None)
    with pytest.raises(SystemExit) as ex:
        tcr.main()
    assert ex.value.code == 2


def test_cli_passes_date_and_window_to_run_send(monkeypatch):
    seen = {}
    monkeypatch.setattr(sys, "argv", ["x", "--send", "--date", "2026-09-30", "--window", "second"])
    monkeypatch.setattr(tcr, "check_token_health", lambda: None)
    monkeypatch.setattr(tcr, "close_db", lambda: None)
    monkeypatch.setattr(tcr, "run_send", lambda test_mode, target_date, part: seen.update(
        test_mode=test_mode, target_date=target_date, part=part))
    tcr.main()
    assert seen == {"test_mode": False, "target_date": date(2026, 9, 30), "part": "second"}
```

- [ ] **Step 2: Run to verify they fail**

Run: `venv\Scripts\python.exe -m pytest tests/test_email_windows.py -q -k "send or cli or previous_day"`
Expected: failures on the new keyword `part` / missing `--window`.

- [ ] **Step 3: Implement**

`get_previous_day_entries`:

```python
def get_previous_day_entries(db, target_date=None, part=None) -> list[dict]:
    """Timer entries for one email window (Eastern date + part).

    Defaults to the last CLOSED window: at the ~18:30 ET send that is
    (today, first); at the ~09:00 ET send it is (yesterday, second). Range
    predicate on start_time so the index is used.
    """
    if (target_date is None) != (part is None):
        raise ValueError("target_date and part must be given together")
    if target_date is None:
        target_date, part = last_closed_window()
    lo, hi = window_bounds(target_date, part)

    rows = retry_db(
        lambda: db.fetch(f"""
            SELECT DISTINCT project_did, project, user_email, start_time, end_time,
                   duration_min, site_name, site_id, task, task_clean, asset_did
            FROM {SCHEMA_STAGING}.stg_timer_activities
            WHERE start_time >= $1 AND start_time < $2
            ORDER BY user_email, site_name, task, start_time
        """, lo, hi),
        description=f"fetch timer entries for window {target_date} {part}",
    )

    return [dict(r) for r in rows] if rows else []
```

`send_daily_emails`: change the signature to `(db, entries, test_mode=False, target_date=None, part=None)`; replace the two lines

```python
    if target_date is None:
        target_date = last_closed_shift_day()
    yesterday = target_date  # ...
```

with

```python
    if (target_date is None) != (part is None):
        raise ValueError("target_date and part must be given together")
    if target_date is None:
        target_date, part = last_closed_window()
    yesterday = target_date  # variable name preserved; it is the Eastern date of the window
```

Insert the callout as the first element after `<p>Hi {_first_name(user_email)},</p>` in the daily HTML: `{coverage_note_html(yesterday, part)}` (one line, where PR #88 placed it). Replace the subject line with `msg["Subject"] = email_subject(yesterday, part)`. Replace the insert with:

```python
                retry_db(
                    lambda ue=user_email, sd=yesterday, w=part, tid=thread_id, mid=message_id,
                           eids=entry_ids_snapshot: db.execute(
                        f"""INSERT INTO {SCHEMA_TIMER}.daily_notifications
                            (user_email, send_date, window, thread_id, message_id,
                             last_sent_at, last_sent_entry_ids)
                            VALUES ($1, $2, $3, $4, $5, NOW(), $6::jsonb)
                            ON CONFLICT (user_email, send_date, window) DO UPDATE SET
                                thread_id = EXCLUDED.thread_id,
                                message_id = EXCLUDED.message_id,
                                last_sent_at = EXCLUDED.last_sent_at,
                                last_sent_entry_ids = EXCLUDED.last_sent_entry_ids
                        """,
                        ue, sd, w, tid, mid, eids,
                    ),
                    description=f"store notification thread for {user_email}",
                )
```

`run_send`:

```python
def run_send(test_mode: bool = False, target_date=None, part=None):
    """Send the timer entry emails for one window and track duplicates."""
    if "PLACEHOLDER" in CORRECT_FORM_ID or "PLACEHOLDER" in REMOVE_FORM_ID:
        logger.warning("Google Form ID is still a placeholder — emails will have broken links.")

    db = get_db()

    if target_date is None:
        target_date, part = last_closed_window()
    logger.info(f"Fetching timer entries for window {target_date} {part} "
                f"({window_label(part)})...")
    entries = get_previous_day_entries(db, target_date=target_date, part=part)

    if not entries:
        logger.info(f"No timer entries found for window {target_date} {part}")
        return

    n_techs = len(set(e['user_email'] for e in entries))
    logger.info(f"Found {len(entries)} entries for {n_techs} techs")

    send_daily_emails(db, entries, test_mode=test_mode, target_date=target_date, part=part)
    detect_and_track_duplicates(db, entries)
```

`main`: replace the `--date` argument and the `target_date` block with

```python
    parser.add_argument("--date", type=str,
                        help="Backfill: Eastern date YYYY-MM-DD of the window to send. "
                             "Requires --window. Default: the last closed window.")
    parser.add_argument("--window", choices=list(WINDOW_PARTS),
                        help="Backfill: which window of --date to send "
                             "(first = 12 AM to 6 PM ET, second = 6 PM to 12 AM ET).")
    args = parser.parse_args()

    if not any([args.send, args.apply, args.remind, args.resend]):
        parser.error("At least one of --send, --apply, --remind, --resend is required")
    if (args.date is None) != (args.window is None):
        parser.error("--date and --window must be given together")

    target_date = date.fromisoformat(args.date) if args.date else None
    part = args.window
```

and call `run_send(test_mode=args.test, target_date=target_date, part=part)`.

- [ ] **Step 4: Run the tests**

Run: `venv\Scripts\python.exe -m pytest tests/test_email_windows.py -q`
Expected: all pass.

- [ ] **Step 5: Commit**

```bash
git add swift_api_pipeline/timer_correction_review.py swift_api_pipeline/tests/test_email_windows.py
git commit -m "feat(timer emails): --send targets an Eastern-day window; --date/--window backfill; window in notification key"
```

---

### Task 4: Workflow dispatch inputs for backfills

**Files:**
- Modify: `.github/workflows/pipeline-timer-emails.yml` (the `on:` block at lines 26-29 and the "send daily entries" step at lines 134-139; header comment lines 1-24)

**Interfaces:**
- Consumes: CLI `--date`/`--window` from Task 3.

- [ ] **Step 1: Edit the trigger block**

```yaml
on:
  repository_dispatch:
    types: [pipeline-timer-emails]
  workflow_dispatch:
    inputs:
      date:
        description: "Backfill only: Eastern date (YYYY-MM-DD) of the window to send. Leave empty for the last closed window."
        required: false
        default: ""
      window:
        description: "Backfill only: which window of that date (first = 12 AM to 6 PM ET, second = 6 PM to 12 AM ET). Required when date is set."
        required: false
        type: choice
        options: ["", "first", "second"]
        default: ""
```

- [ ] **Step 2: Edit the send step**

```yaml
      - name: Timer corrections - send daily entries
        working-directory: ${{ env.PIPELINE_DIR }}
        continue-on-error: true
        # Target = the last closed window: (today, first) at the 18:30 ET run,
        # (yesterday, second) at the 09:00 ET run. A manual run may name a
        # window explicitly for backfills.
        env:
          BF_DATE: ${{ github.event.inputs.date }}
          BF_WINDOW: ${{ github.event.inputs.window }}
        run: |
          if [ -n "$BF_DATE" ]; then
            python -u timer_correction_review.py --send --date "$BF_DATE" --window "$BF_WINDOW"
          else
            python -u timer_correction_review.py --send
          fi
```

- [ ] **Step 3: Update the header comment** (lines 1-24): replace the shift-day sentences with "Fired twice a day by Apps Script on the Eastern clock: ~18:30 ET (first window, 12 AM to 6 PM ET) and ~09:00 ET (second window, 6 PM to 12 AM ET of the previous Eastern date). `--send` picks the last closed window by the clock." Keep the NOTE about `--send` re-sending on a second run.

- [ ] **Step 4: Validate YAML**

Run: `venv\Scripts\python.exe -c "import yaml,sys; yaml.safe_load(open('../.github/workflows/pipeline-timer-emails.yml')); print('yaml ok')"` (from `swift_api_pipeline/`; `pyyaml` is in requirements).
Expected: `yaml ok`.

- [ ] **Step 5: Commit**

```bash
git add .github/workflows/pipeline-timer-emails.yml
git commit -m "ci(timer emails): date/window dispatch inputs for window backfills"
```

---

### Task 5: Replies, confirmations, reminders and resend on windows

**Files:**
- Modify: `swift_api_pipeline/timer_correction_review.py` — `_resolve_stale_response` (~1509, no change needed beyond Task 1), `applied_changes` builders (~1931, 1970, 2040, 2075), `_fetch_classified_day_entries` (~2370), `send_correction_confirmations` (~2823-2870), `_build_correction_confirmation_html` (~2780-2815, add callout), reminders (~2960-3020), `_fetch_current_day_entries` (~3184), `find_days_needing_resend` (~3212-3310), `send_resend_emails` (~3413-3550)
- Test: `swift_api_pipeline/tests/test_email_windows.py` (append)

**Interfaces:**
- Consumes: Task 1 helpers; `window` column from Task 2.
- Produces:
  - `_fetch_classified_day_entries(db, user_email, entry_date, part)`
  - `_fetch_current_day_entries(db, user_email, send_date, window)` where `window` may be `"full"`
  - `applied_changes[*]["entry_window"]: str`
  - resend candidates carry `"window"`.

- [ ] **Step 1: Write the failing tests** (append)

```python
# ---- downstream wiring ------------------------------------------------------

def test_resolve_stale_response_uses_form_lookup_bounds():
    db = _RecordingDB()
    resp = {"entry_id": "deadbeef", "respondent": "a@ontel.co",
            "details": "Proj | Site A | 6. Final COP | Sep 30, 2026 | 1h 30m"}
    tcr._resolve_stale_response(db, resp)
    lo, hi = form_lookup_bounds(date(2026, 9, 30))
    assert db.params[0] == lo and db.params[1] == hi
    assert "start_time >= $1 AND start_time < $2" in db.sql


def test_fetch_classified_day_entries_uses_window_bounds():
    db = _RecordingDB()
    tcr._fetch_classified_day_entries(db, "a@ontel.co", date(2026, 9, 30), "second")
    assert db.sql.count("start_time >= $2 AND") == 2
    assert db.sql.count("start_time < $3") == 2
    lo, hi = window_bounds(date(2026, 9, 30), "second")
    assert db.params == ("a@ontel.co", lo, hi)


def test_fetch_current_day_entries_window_and_legacy():
    db = _RecordingDB()
    tcr._fetch_current_day_entries(db, "a@ontel.co", date(2026, 9, 30), "first")
    assert db.params == ("a@ontel.co",) + window_bounds(date(2026, 9, 30), "first")
    tcr._fetch_current_day_entries(db, "a@ontel.co", date(2026, 9, 30), "full")
    assert db.params == ("a@ontel.co",) + legacy_full_bounds(date(2026, 9, 30))


def test_find_days_needing_resend_uses_row_window(monkeypatch):
    class _DB(_RecordingDB):
        def fetch(self, sql, *params):
            super().fetch(sql, *params)
            if "FROM" in sql and "daily_notifications" in sql:
                return [
                    {"user_email": "a@ontel.co", "send_date": date(2026, 9, 30), "window": "full",
                     "thread_id": "t1", "message_id": "m1", "last_sent_at": None, "last_sent_entry_ids": None},
                    {"user_email": "a@ontel.co", "send_date": date(2026, 9, 30), "window": "second",
                     "thread_id": "t2", "message_id": "m2", "last_sent_at": None, "last_sent_entry_ids": None},
                ]
            return []
    seen = []
    monkeypatch.setattr(tcr, "_fetch_current_day_entries",
                        lambda db, ue, sd, w: seen.append((sd, w)) or [])
    db = _DB()
    tcr.find_days_needing_resend(db, lookback_days=7)
    assert seen == [(date(2026, 9, 30), "full"), (date(2026, 9, 30), "second")]
    select_sql = [c for c in db.calls if "daily_notifications" in c[0]][0][0]
    assert "SELECT user_email, send_date, window, thread_id, message_id," in select_sql
    cutoff = window_of(datetime.now(timezone.utc))[0] - timedelta(days=7)
    assert db.calls[0][1] == (cutoff,)


def test_applied_change_records_carry_entry_window():
    lo, _ = window_bounds(date(2026, 9, 30), "second")
    rec = tcr._change_record("eid1", "correct", {"user_email": "a@ontel.co", "start_time": lo + timedelta(minutes=1),
                                                 "duration_min": 30}, corrected_duration_min=45)
    assert rec["entry_date"] == date(2026, 9, 30)
    assert rec["entry_window"] == "second"
    assert rec["original_duration_min"] == 30 and rec["corrected_duration_min"] == 45
```

- [ ] **Step 2: Run to verify they fail**

Run: `venv\Scripts\python.exe -m pytest tests/test_email_windows.py -q -k "stale or classified or current_day or resend or applied_change"`
Expected: failures (`shift_day` NameError, missing `part` parameter, missing `_change_record`).

- [ ] **Step 3: Implement**

Add a small factory above the first `applied_changes.append` site (around line 1960) and use it at all four sites (lines ~1965-1973, ~2036-2043, ~2071-2078, plus the remove site that stores the first row under the stale id):

```python
def _change_record(entry_id: str, action: str, entry: dict,
                   corrected_duration_min=None) -> dict:
    """One applied-change record; the window is derived from the entry's start."""
    day, part = window_of(entry["start_time"])
    return {
        "entry_id": entry_id,
        "action": action,
        "user_email": entry["user_email"],
        "entry_date": day,
        "entry_window": part,
        "entry": entry,
        "original_duration_min": entry.get("duration_min"),
        "corrected_duration_min": corrected_duration_min,
    }
```

Each existing literal dict becomes `applied_changes.append(_change_record(<id>, "<action>", <row>, corrected_duration_min=<value or None>))`. The warning at ~1931 uses `window_of(group[0]['start_time'])[0]` in place of `shift_day(...)`.

`_fetch_classified_day_entries(db, user_email, entry_date, part)`: replace `lo, hi = shift_day_bounds(entry_date)` with `lo, hi = window_bounds(entry_date, part)`.

`send_correction_confirmations`: group by `(change["user_email"], change["entry_date"], change["entry_window"])`; loop `for (user_email, entry_date, part), changes in ...`; notification lookup becomes

```python
                f"""SELECT thread_id, message_id
                    FROM {SCHEMA_TIMER}.daily_notifications
                    WHERE user_email = $1 AND send_date = $2 AND window = $3
                """, ue, sd, w,
```

with `w=part` bound in the lambda; call `_fetch_classified_day_entries(db, user_email, entry_date, part)`; pass `part` into `_build_correction_confirmation_html(user_email, entry_date, classified, part=part, ...)` and in that builder insert `{coverage_note_html(entry_date, part)}` right after the `Hi {name}` paragraph; subject stays `Timer Entries Updated - {date_str}` with ` ({window_label(part)})` appended.

Reminders (~2960-3020): `entry_date, part = window_of(r["start_time"])`, key `(r["user_email"], entry_date, part)`; the thread lookup adds `AND window = $3` with `w=part`; the test-mode filter uses `(send_date, window)` pairs: `dates_with_notifs = {(r["send_date"], r["window"]) for r in notif_dates}` with `SELECT DISTINCT send_date, window`, filter `k[1:] in dates_with_notifs`. Where the reminder HTML names the date, append ` ({window_label(part)})`.

`_fetch_current_day_entries(db, user_email, send_date, window)`:

```python
    lo, hi = legacy_full_bounds(send_date) if window == LEGACY_WINDOW else window_bounds(send_date, window)
```

`find_days_needing_resend`: cutoff `window_of(datetime.now(timezone.utc))[0] - timedelta(days=lookback_days)`; SELECT adds `window` after `send_date`; `current_all = _fetch_current_day_entries(db, user_email, send_date, r["window"])`; the bootstrap UPDATE and the candidate dict carry `window` (`WHERE user_email = $2 AND send_date = $3 AND window = $4`, bound `w=r["window"]`); candidates gain `"window": r["window"]`.

`send_resend_emails`: `window = c["window"]`; the post-send UPDATE adds `AND window = $4`; insert `{coverage_note_html(send_date, window) if window != LEGACY_WINDOW else ""}` before `{callout_html}`; the header `<h2>` and subject get ` ({window_label(window)})` appended when `window != LEGACY_WINDOW`.

- [ ] **Step 4: Run the whole file and the other timer tests**

Run: `venv\Scripts\python.exe -m pytest tests/test_email_windows.py tests/test_timer_carry_forward.py tests/test_timer_overlap.py tests/test_dr_timer_fetch_policy.py -q`
Expected: all pass. Then `venv\Scripts\python.exe -c "import timer_correction_review"` and `grep -n "shift_day" timer_correction_review.py` must print nothing except comments.

- [ ] **Step 5: Commit**

```bash
git add swift_api_pipeline/timer_correction_review.py swift_api_pipeline/tests/test_email_windows.py
git commit -m "feat(timer emails): confirmations, reminders and resends keyed by Eastern-day window"
```

---

### Task 6: Apps Script triggers on the Eastern clock

**Files:**
- Modify: `scripts/pipeline_trigger.gs:150-205` (the timer-emails block) and the header schedule list (lines 1-60)

**Interfaces:**
- Produces: `triggerTimerEmails()` (unchanged dispatch), `triggerTimerEmailsSecond()`, `setupTimerEmailsTrigger()` recreating both.

- [ ] **Step 1: Replace the block**

```javascript
/**
 * Timer EMAILS run, twice a day on the EASTERN clock (since 2026-10-01):
 *   ~18:30 ET  triggerTimerEmails()        -> window (today, first):  12 AM to 6 PM ET
 *   ~09:00 ET  triggerTimerEmailsSecond()  -> window (yesterday, second): 6 PM to 12 AM ET
 * Both fire the same 'pipeline-timer-emails' dispatch; the Python side picks
 * the last CLOSED window by the clock, so the trigger only has to land after
 * the window closes. Pinned to America/New_York on purpose: the windows are
 * Eastern wall-clock, so a Manila-pinned trigger would fire BEFORE 6 PM ET
 * in winter and the run would pick the wrong window.
 *
 * Minute 30 / minute 0 with +/-15 min jitter keeps both runs after their
 * window close (18:15-18:45, 08:45-09:15).
 *
 * This is run 2 of 2 for timer data: it re-extracts, applies corrections,
 * rebuilds the clean table, then sends the member-facing emails
 * (--remind / --send / --resend). Run 1 (triggerLightPipelines, ~1:15 AM
 * ET) is data + Excel exports only since 2026-08-28.
 *
 * DIFFERENT dispatch type from 'pipeline-timer' on purpose: the two runs must
 * never be fired by the same trigger.
 */
var TIMER_EMAILS_FIRST_HOUR = 18;    // America/New_York
var TIMER_EMAILS_FIRST_MINUTE = 30;
var TIMER_EMAILS_SECOND_HOUR = 9;    // America/New_York
var TIMER_EMAILS_SECOND_MINUTE = 0;

function triggerTimerEmails() {
  fireDispatch_('pipeline-timer-emails');
}

function triggerTimerEmailsSecond() {
  fireDispatch_('pipeline-timer-emails');
}

/**
 * Idempotently (re)create BOTH daily time-driven triggers. RUN THIS ONCE from
 * the Apps Script editor after deploying this file (and again after editing
 * the constants); it deletes any existing trigger on either handler first.
 *
 * CUTOVER (2026-10-01): run after migration 275 + the two-window PR are live
 * and BEFORE 18:15 ET on 2026-10-01, so the first Eastern-clock send covers
 * (2026-10-01, first).
 */
function setupTimerEmailsTrigger() {
  var handlers = ['triggerTimerEmails', 'triggerTimerEmailsSecond'];
  var existing = ScriptApp.getProjectTriggers();
  for (var i = 0; i < existing.length; i++) {
    if (handlers.indexOf(existing[i].getHandlerFunction()) !== -1) {
      ScriptApp.deleteTrigger(existing[i]);
    }
  }

  ScriptApp.newTrigger('triggerTimerEmails')
    .timeBased().everyDays(1)
    .atHour(TIMER_EMAILS_FIRST_HOUR).nearMinute(TIMER_EMAILS_FIRST_MINUTE)
    .inTimezone('America/New_York')
    .create();
  ScriptApp.newTrigger('triggerTimerEmailsSecond')
    .timeBased().everyDays(1)
    .atHour(TIMER_EMAILS_SECOND_HOUR).nearMinute(TIMER_EMAILS_SECOND_MINUTE)
    .inTimezone('America/New_York')
    .create();

  Logger.log('Created triggerTimerEmails at ~18:30 America/New_York and ' +
             'triggerTimerEmailsSecond at ~09:00 America/New_York (daily, +/-15 min).');
}
```

- [ ] **Step 2: Update the header list** (the `triggerTimerEmails` line in the setup list and the Schedules list): "`triggerTimerEmails()` → Daily ~6:30 PM ET (first window) and `triggerTimerEmailsSecond()` → Daily ~9:00 AM ET (second window); create both via `setupTimerEmailsTrigger()`."

- [ ] **Step 3: Sanity check with node**

Run: `node -e "const fs=require('fs');const src=fs.readFileSync('scripts/pipeline_trigger.gs','utf8');new Function(src);console.log('parses')"` (from the repo root).
Expected: `parses`. Also `grep -c "function fireDispatch_" scripts/pipeline_trigger.gs` prints `1`.

- [ ] **Step 4: Commit**

```bash
git add scripts/pipeline_trigger.gs
git commit -m "apps script: timer emails twice daily on the Eastern clock (18:30 first, 09:00 second)"
```

---

### Task 7: Docs, work log, PR #88 closure

**Files:**
- Modify: `README.md` (line 35 table row; lines 111-128 paragraph), `swift_api_pipeline/README.md` (shift-day mentions), `WORK_LOG.md` (append), `docs/superpowers/specs/2026-09-23-timer-emails-shift-day-design.md` (add a one-line "Superseded by ..." note at the top)

- [ ] **Step 1: README row** — replace the `pipeline-timer-emails.yml` row with: `| pipeline-timer-emails.yml | Apps Script ~18:30 ET (triggerTimerEmails) and ~09:00 ET (triggerTimerEmailsSecond), both pinned to America/New_York | Run 2 of 2: re-extract through today ET + backfill + apply/rebuild + MV refresh, then the member-facing emails (--remind, --send, --resend) for the last closed window: first = 12 AM to 6 PM ET (sent 18:30 ET), second = 6 PM to 12 AM ET (sent 09:00 ET next day). Manual backfill: workflow inputs date + window. Separate dispatch type; same pipeline-timer concurrency group. No exports |`

- [ ] **Step 2: README paragraph** (lines 111-128): rewrite to describe the two windows, the Eastern date label, the callout, the `--date/--window` backfill, and point to the new spec. Keep the sentence that DRMC, variance and exports stay on ET calendar dates (now the same day as the email).

- [ ] **Step 3: Old spec note** — first line after the title of `2026-09-23-timer-emails-shift-day-design.md`: `> Superseded 2026-10-01 by 2026-10-01-timer-email-two-windows-design.md (two windows per Eastern day).`

- [ ] **Step 4: WORK_LOG entry** — append under a header `## 2026-10-01 (<start> - <end> ET) - Timer Entries email: two windows per Eastern day (mig 275)` with: why, what changed (helpers, key, CLI, workflow inputs, triggers), test counts from the final run, cutover steps performed with run ids, and the follow-ups (health watcher dispatch staleness; remaining ontel Apps Script PAT rows).

- [ ] **Step 5: Commit, push, open PR, close #88**

```bash
git add README.md swift_api_pipeline/README.md WORK_LOG.md docs/superpowers/specs/2026-09-23-timer-emails-shift-day-design.md
git commit -m "docs(timer emails): two windows per Eastern day; supersede the shift-day spec"
git push -u origin feat/timer-email-two-windows
gh pr create --title "Timer Entries email: two windows per Eastern day (mig 275)" --body-file <(printf '%s\n' "Spec: docs/superpowers/specs/2026-10-01-timer-email-two-windows-design.md" "" "Absorbs and closes #88." "" "🤖 Generated with [Claude Code](https://claude.com/claude-code)")
gh pr close 88 --comment "Superseded by the two-window PR; the callout lives there."
```

---

### Task 8: Pre-merge review, merge, migration, backfill (cutover)

**Files:** none new. Uses the `premerge-review` project skill.

- [ ] **Step 1:** Invoke the `premerge-review` skill on the PR (code review + resilience audit + DB preflight for migration 275). Fix findings, re-run `venv\Scripts\python.exe -m pytest tests -q` and record the numbers.
- [ ] **Step 2:** Squash-merge to main.
- [ ] **Step 3:** Apply migration 275 to OntelDB (Supabase MCP `apply_migration` after re-auth, or psql via the IPv4 pooler). Verify: `SELECT conname FROM pg_constraint WHERE conrelid='app_timer.daily_notifications'::regclass;` shows the three-column key; `SELECT window, count(*) FROM app_timer.daily_notifications GROUP BY 1;` shows only `full`.
- [ ] **Step 4:** Backfill: `gh workflow run pipeline-timer-emails.yml -R jamilmendez-ontel/local-pipeline -f date=2026-09-30 -f window=second`; watch; confirm the log line `Fetching timer entries for window 2026-09-30 second` and the send count; confirm rows `window='second'` for 2026-09-30.
- [ ] **Step 5:** Hand Jamil `scripts/pipeline_trigger.gs` to paste whole-file into nanoninth and run `setupTimerEmailsTrigger()` before 18:15 ET; confirm in the Triggers pane: two handlers, America/New_York.
- [ ] **Step 6:** Verify tonight's 18:30 ET run logs `window 2026-10-01 first` and tomorrow's 09:00 ET run logs `window 2026-10-01 second`.
