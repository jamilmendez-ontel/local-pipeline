# Timer Entries Email on a 06:00 PHT Shift Day: Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Move the member-facing Timer Entries email from 18:00 PHT to ~06:30 PHT and bucket it on a 06:00-to-06:00 Asia/Manila shift day instead of the ET calendar date, so the email sent 06:30 PHT 9/23 covers 06:00 PHT 9/22 to 06:00 PHT 9/23.

**Architecture:** One pair of pure helpers (`shift_day`, `shift_day_bounds`) replaces every ET-calendar-date bucketing site in `timer_correction_review.py`, and SQL moves from `DATE(start_time AT TIME ZONE 'America/New_York') = $1` to a sargable `start_time >= $lo AND start_time < $hi` range. The Apps Script trigger is pinned to Asia/Manila. A one-off snapshot reset at cutover stops `--resend` from misreading the definition change as new entries.

**Tech Stack:** Python 3.12 (`zoneinfo`, `asyncpg` via `config.get_db`), pytest, Google Apps Script (`pipeline_trigger.gs`), GitHub Actions (`repository_dispatch`).

**Spec:** `docs/superpowers/specs/2026-09-23-timer-emails-shift-day-design.md`

## Global Constraints

- Scope is `pipeline-timer-emails.yml` and what it runs. `pipeline-timer.yml` (13:15 PHT data + exports), DRMC, variance, exports, MVs and `reference.*` are not touched.
- Shift day D = `[06:00 Asia/Manila D, 06:00 Asia/Manila D+1)`, labelled D. `05:59:59 PHT 9/23` is shift day 9/22; `06:00:00 PHT 9/23` is shift day 9/23.
- Naive datetimes are UTC, matching every other helper in the file.
- Email subject/header format `"%B %d, %Y"` is unchanged; the date it shows is the shift day.
- `app_timer.daily_notifications.send_date` keeps meaning "the date in the email header". No migration.
- Trigger: `atHour(6).nearMinute(30).inTimezone('Asia/Manila')`.
- Any ad-hoc email send is `--test` mode only, which delivers to `jamil.mendez@ontel.co`.
- Test baseline on `ed3671a`: 314 passed, 6 failed, all six in `tests/test_asset_tasks_resilience.py` (pre-existing, unrelated). Do not touch them; the bar is "no new failures".
- Run the suite from `swift_api_pipeline/`: `python -m pytest -q -p no:warnings --ignore=tests/render_running_entries_sample.py`.
- Commit messages end with `Co-Authored-By: Claude Fable 5.1 <noreply@anthropic.com>`.
- No em-dashes in comments, docs or copy.

---

## File map

| file | responsibility in this change |
|---|---|
| `swift_api_pipeline/timer_correction_review.py` | shift-day helpers; all bucketing sites for `--send`, `--apply` change records, `--remind`, `--resend` |
| `swift_api_pipeline/tests/test_shift_day.py` (new) | unit tests for the helpers and the default target |
| `scripts/pipeline_trigger.gs` | `TIMER_EMAILS_HOUR/MINUTE`, `setupTimerEmailsTrigger()` pinned to Asia/Manila, comments |
| `.github/workflows/pipeline-timer-emails.yml` | header comment: time, window, target |
| `.github/workflows/pipeline-timer.yml` | the "member-facing emails moved to ..." note |
| `README.md` | workflow table row (line 35) and the scheduling paragraph (lines 108 to 118) |
| `WORK_LOG.md` | session entry with the cutover checklist |

---

### Task 1: Shift-day helpers

**Files:**
- Modify: `swift_api_pipeline/timer_correction_review.py:48-56` (imports and TZ block)
- Create: `swift_api_pipeline/tests/test_shift_day.py`

**Interfaces:**
- Consumes: nothing new. `TZ_EASTERN`, `timezone`, `datetime`, `timedelta` already exist at the top of the file.
- Produces:
  - `TZ_MANILA = ZoneInfo("Asia/Manila")`
  - `SHIFT_DAY_START_HOUR = 6`
  - `shift_day(dt: datetime | str) -> date`
  - `shift_day_bounds(day: date) -> tuple[datetime, datetime]` (tz-aware UTC, half-open)
  - `form_lookup_bounds(day: date) -> tuple[datetime, datetime]` (tz-aware UTC, union of the old ET-day and new shift-day windows)
  - `last_closed_shift_day(now: datetime | None = None) -> date`

- [ ] **Step 1: Check what `datetime` names are imported**

Run: `grep -n "^from datetime import" swift_api_pipeline/timer_correction_review.py`

Expected: a line like `from datetime import datetime, timedelta, timezone`. If `date` is not in that list, Step 3 adds it. (Line 353 currently annotates `-> "date"` as a string precisely because it is not imported.)

- [ ] **Step 2: Write the failing tests**

Create `swift_api_pipeline/tests/test_shift_day.py`:

```python
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
    assert lo == sd_lo                      # opens with the shift day
    assert hi == et_hi.astimezone(timezone.utc)  # closes with the ET day
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
```

- [ ] **Step 3: Run the tests to verify they fail**

Run: `cd swift_api_pipeline && python -m pytest tests/test_shift_day.py -q -p no:warnings`

Expected: FAIL at import with `ImportError: cannot import name 'TZ_MANILA'`.

- [ ] **Step 4: Add the helpers**

In `swift_api_pipeline/timer_correction_review.py`, make sure the datetime import includes `date`:

```python
from datetime import date, datetime, timedelta, timezone
```

Then replace the `TZ_EASTERN = ZoneInfo("America/New_York")` line (currently line 54) with:

```python
TZ_EASTERN = ZoneInfo("America/New_York")
TZ_MANILA = ZoneInfo("Asia/Manila")

# The member-facing Timer Entries email is bucketed on a SHIFT DAY: the 24
# hours from 06:00 Asia/Manila on date D to 06:00 Asia/Manila on D+1,
# labelled D (the date the 18:00 PHT shift starts). Before 2026-09-28 it was
# bucketed on the ET calendar date (noon PHT to noon PHT), which disagreed
# with the members' 06:00 PHT shift end on ~10% of entries (the 06:00-12:00
# PHT overtime band). Asia/Manila has no DST, so the boundary never moves.
# Everything the emails run touches (--send, --remind, --resend, the change
# records --apply writes) uses these helpers; nothing downstream (DRMC,
# variance, exports) does, they stay on ET calendar dates.
SHIFT_DAY_START_HOUR = 6


def _as_aware_utc(dt) -> datetime:
    """ISO string or datetime -> tz-aware datetime; naive means UTC."""
    if isinstance(dt, str):
        dt = datetime.fromisoformat(dt)
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt


def shift_day(dt) -> date:
    """Shift day an instant belongs to: the Asia/Manila date of (dt - 6h)."""
    local = _as_aware_utc(dt).astimezone(TZ_MANILA)
    return (local - timedelta(hours=SHIFT_DAY_START_HOUR)).date()


def shift_day_bounds(day: date) -> tuple[datetime, datetime]:
    """[06:00 PHT day, 06:00 PHT day+1) as tz-aware UTC datetimes.

    Use as `start_time >= $lo AND start_time < $hi`: sargable on the
    start_time index, unlike the DATE(... AT TIME ZONE ...) = $1 form.
    """
    lo = datetime(day.year, day.month, day.day, SHIFT_DAY_START_HOUR,
                  tzinfo=TZ_MANILA).astimezone(timezone.utc)
    return lo, lo + timedelta(days=1)


def form_lookup_bounds(day: date) -> tuple[datetime, datetime]:
    """Window for resolving a member's form reply that names `day`.

    The union of the OLD ET-calendar-day window and the NEW shift-day
    window: opens at 06:00 PHT `day`, closes at 00:00 ET `day`+1 (30 h).
    Replies to emails sent before the 2026-09-28 cutover carry ET-day
    dates; replies after it carry shift-day dates. The group lookup also
    matches on site/task/start, so the extra hours cannot make it
    ambiguous. Safe to keep forever.
    """
    lo, _ = shift_day_bounds(day)
    et_end = (datetime(day.year, day.month, day.day, tzinfo=TZ_EASTERN)
              + timedelta(days=1)).astimezone(timezone.utc)
    return lo, et_end


def last_closed_shift_day(now: datetime | None = None) -> date:
    """The most recent shift day whose window has fully closed.

    At the ~06:30 PHT send this is yesterday's shift day. If a run ever
    fired before 06:00 PHT it would (correctly) target the day before that
    rather than email a still-open window.
    """
    now = _as_aware_utc(now or datetime.now(timezone.utc))
    return shift_day(now) - timedelta(days=1)
```

- [ ] **Step 5: Run the tests to verify they pass**

Run: `cd swift_api_pipeline && python -m pytest tests/test_shift_day.py -q -p no:warnings`

Expected: `17 passed`.

- [ ] **Step 6: Run the full suite**

Run: `cd swift_api_pipeline && python -m pytest -q -p no:warnings --ignore=tests/render_running_entries_sample.py 2>&1 | tail -3`

Expected: `6 failed, 331 passed` (baseline 314 + 17 new; the 6 are the pre-existing `test_asset_tasks_resilience` failures).

- [ ] **Step 7: Commit**

```bash
git add swift_api_pipeline/timer_correction_review.py swift_api_pipeline/tests/test_shift_day.py
git commit -m "timer emails: shift-day helpers (06:00 Asia/Manila)

shift_day / shift_day_bounds / form_lookup_bounds / last_closed_shift_day
with boundary, DST-independence and round-trip tests. Not wired in yet.

Co-Authored-By: Claude Fable 5.1 <noreply@anthropic.com>"
```

---

### Task 2: `--send` path and the `--apply` change records

**Files:**
- Modify: `swift_api_pipeline/timer_correction_review.py`
  - `_entry_date_et` at :353-359 and its four callers at :1872, :1911, :1981, :2016
  - `get_previous_day_entries` at :656-676
  - `send_daily_emails` default target at :1021-1024
  - `run_send` label at :1300
  - `--date` help text at :3518

**Interfaces:**
- Consumes: `shift_day`, `shift_day_bounds`, `last_closed_shift_day` from Task 1.
- Produces: `get_previous_day_entries(db, target_date=None)` now returns the shift day's entries; `target_date` everywhere means a shift day.

- [ ] **Step 1: Write the failing tests**

Append to `swift_api_pipeline/tests/test_shift_day.py`:

```python
def test_entry_date_et_is_gone():
    # The ET-calendar helper must not survive; every caller moved to shift_day.
    import timer_correction_review as tcr
    assert not hasattr(tcr, "_entry_date_et")


def test_get_previous_day_entries_uses_shift_day_bounds():
    """The --send query must be a half-open range on the shift day."""
    import timer_correction_review as tcr

    captured = {}

    class FakeDB:
        def fetch(self, sql, *params):
            captured["sql"] = sql
            captured["params"] = params
            return []

    tcr.get_previous_day_entries(FakeDB(), target_date=date(2026, 9, 22))
    sql = " ".join(captured["sql"].split())
    assert "start_time >= $1 AND start_time < $2" in sql
    assert "America/New_York" not in sql
    assert captured["params"] == shift_day_bounds(date(2026, 9, 22))


def test_get_previous_day_entries_default_is_last_closed_shift_day():
    import timer_correction_review as tcr

    captured = {}

    class FakeDB:
        def fetch(self, sql, *params):
            captured["params"] = params
            return []

    tcr.get_previous_day_entries(FakeDB())
    assert captured["params"] == shift_day_bounds(last_closed_shift_day())
```

`retry_db` calls the lambda directly (it wraps `db.fetch`), so a plain fake with a synchronous `fetch` is enough. If `retry_db` awaits, check `config.retry_db` and make `fetch` an `async def` returning `[]` instead; the assertions do not change.

- [ ] **Step 2: Run the tests to verify they fail**

Run: `cd swift_api_pipeline && python -m pytest tests/test_shift_day.py -q -p no:warnings -k "entry_date_et or previous_day"`

Expected: 3 FAIL (`_entry_date_et` still exists; SQL still contains `America/New_York`).

- [ ] **Step 3: Replace `_entry_date_et`**

Delete the whole `_entry_date_et` function at :353-359:

```python
def _entry_date_et(dt) -> "date":
    """Return the calendar date of dt interpreted in Eastern Time."""
    if isinstance(dt, str):
        dt = datetime.fromisoformat(dt)
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(TZ_EASTERN).date()
```

Then at each of the four call sites (:1872, :1911, :1981, :2016) change `_entry_date_et(` to `shift_day(`. The lines become:

```python
                        f"{shift_day(group[0]['start_time'])}; "
```
```python
                        "entry_date": shift_day(row["start_time"]),
```
```python
                "entry_date": shift_day(entry["start_time"]),
```
```python
                "entry_date": shift_day(entry["start_time"]),
```

Verify nothing else references it: `grep -n "_entry_date_et" swift_api_pipeline/timer_correction_review.py` must print nothing.

- [ ] **Step 4: Rewrite `get_previous_day_entries`**

Replace :656-676 with:

```python
def get_previous_day_entries(db, target_date=None) -> list[dict]:
    """Timer entries for one shift day (06:00 PHT to 06:00 PHT).

    Defaults to the last CLOSED shift day, which at the ~06:30 PHT send is
    yesterday's. Range predicate on start_time so the index is used.
    """
    if target_date is None:
        target_date = last_closed_shift_day()
    lo, hi = shift_day_bounds(target_date)

    rows = retry_db(
        lambda: db.fetch(f"""
            SELECT DISTINCT project_did, project, user_email, start_time, end_time,
                   duration_min, site_name, site_id, task, task_clean, asset_did
            FROM {SCHEMA_STAGING}.stg_timer_activities
            WHERE start_time >= $1 AND start_time < $2
            ORDER BY user_email, site_name, task, start_time
        """, lo, hi),
        description=f"fetch timer entries for shift day {target_date}",
    )

    return [dict(r) for r in rows] if rows else []
```

- [ ] **Step 5: Fix the default in `send_daily_emails`**

At :1021-1024 replace:

```python
    if target_date is None:
        target_date = (datetime.now(TZ_EASTERN) - timedelta(days=1)).date()
    yesterday = target_date  # variable name preserved — used as the entry date below
    date_str = yesterday.strftime("%B %d, %Y")
```

with:

```python
    if target_date is None:
        target_date = last_closed_shift_day()
    yesterday = target_date  # variable name preserved; it is the shift day (spec 3.1)
    date_str = yesterday.strftime("%B %d, %Y")
```

- [ ] **Step 6: Fix the label in `run_send` and the CLI help**

At :1300 replace `date_label = target_date or "previous day"` with:

```python
    date_label = target_date or f"shift day {last_closed_shift_day()}"
```

At :1302-1304 the "No timer entries found for previous day" log becomes:

```python
        logger.info(f"No timer entries found for {date_label}")
```

At :3518 replace the `--date` help with:

```python
    parser.add_argument("--date", type=str,
                        help="Target SHIFT DAY YYYY-MM-DD, i.e. the date the 18:00 PHT shift "
                             "started (default: the last closed shift day). For backfill sends.")
```

- [ ] **Step 7: Run the tests to verify they pass**

Run: `cd swift_api_pipeline && python -m pytest tests/test_shift_day.py -q -p no:warnings`

Expected: `20 passed`.

- [ ] **Step 8: Run the full suite**

Run: `cd swift_api_pipeline && python -m pytest -q -p no:warnings --ignore=tests/render_running_entries_sample.py 2>&1 | tail -3`

Expected: `6 failed, 334 passed`, same six.

- [ ] **Step 9: Commit**

```bash
git add swift_api_pipeline/timer_correction_review.py swift_api_pipeline/tests/test_shift_day.py
git commit -m "timer emails: --send targets the last closed shift day

get_previous_day_entries uses a start_time range on shift_day_bounds;
send_daily_emails / run_send default to last_closed_shift_day(); the
--apply change records use shift_day instead of the ET date. _entry_date_et
removed.

Co-Authored-By: Claude Fable 5.1 <noreply@anthropic.com>"
```

---

### Task 3: Form-reply group lookup uses the widened window

**Files:**
- Modify: `swift_api_pipeline/timer_correction_review.py` `_resolve_stale_response` at :1435-1456

**Interfaces:**
- Consumes: `form_lookup_bounds` from Task 1.
- Produces: nothing new; the function keeps its signature.

- [ ] **Step 1: Write the failing test**

Append to `swift_api_pipeline/tests/test_shift_day.py`:

```python
def test_resolve_stale_response_uses_form_lookup_bounds():
    """A member's reply names a date; look it up across BOTH day definitions."""
    import timer_correction_review as tcr

    captured = {}

    class FakeDB:
        def fetch(self, sql, *params):
            captured["sql"] = sql
            captured["params"] = params
            return []

    # Whatever _parse_entry_details expects, feed it a start_date directly.
    original = tcr._parse_entry_details
    tcr._parse_entry_details = lambda _details: {
        "start_date": date(2026, 9, 22), "site_name": "X", "task": "Y",
        "start_time_str": "", "duration_str": "",
    }
    try:
        tcr._resolve_stale_response(FakeDB(), {"details": "anything"})
    finally:
        tcr._parse_entry_details = original

    lo, hi = form_lookup_bounds(date(2026, 9, 22))
    assert captured["params"][0] == lo
    assert captured["params"][1] == hi
```

Before running, read `_parse_entry_details` (search `def _parse_entry_details`) and adjust the fake's returned keys to whatever `_resolve_stale_response` reads after the date. The assertion is only on the first two query parameters.

- [ ] **Step 2: Run the test to verify it fails**

Run: `cd swift_api_pipeline && python -m pytest tests/test_shift_day.py -q -p no:warnings -k resolve_stale`

Expected: FAIL, `captured["params"][0]` is `00:00 ET 9/22` (04:00 UTC), not `06:00 PHT 9/22` (21 Sep 22:00 UTC).

- [ ] **Step 3: Widen the window**

At :1448-1450 replace:

```python
    day_start = datetime(parsed["start_date"].year, parsed["start_date"].month,
                         parsed["start_date"].day, tzinfo=TZ_EASTERN)
    day_end = day_start + timedelta(days=1)
```

with:

```python
    # Union of the pre-2026-09-28 ET-day window and the shift-day window
    # for the date the member's reply names; see form_lookup_bounds.
    day_start, day_end = form_lookup_bounds(parsed["start_date"])
```

Leave the `WHERE start_time >= $1 AND start_time < $2` query as is.

- [ ] **Step 4: Run the tests to verify they pass**

Run: `cd swift_api_pipeline && python -m pytest tests/test_shift_day.py -q -p no:warnings`

Expected: `21 passed`.

- [ ] **Step 5: Run the full suite**

Run: `cd swift_api_pipeline && python -m pytest -q -p no:warnings --ignore=tests/render_running_entries_sample.py 2>&1 | tail -3`

Expected: `6 failed, 335 passed`, same six. `test_stale_fallback.py` in particular must still pass; it exercises `_resolve_stale_response`.

- [ ] **Step 6: Commit**

```bash
git add swift_api_pipeline/timer_correction_review.py swift_api_pipeline/tests/test_shift_day.py
git commit -m "timer emails: form-reply lookup spans both day definitions

_resolve_stale_response looks up the named date over the union of the old
ET-day and new shift-day windows, so replies to pre-cutover emails resolve.

Co-Authored-By: Claude Fable 5.1 <noreply@anthropic.com>"
```

---

### Task 4: `--remind` classification and grouping

**Files:**
- Modify: `swift_api_pipeline/timer_correction_review.py`
  - `_fetch_classified_day_entries` at :2295-2380 (two `DATE(... America/New_York) = $2` predicates at :2319 and :2373)
  - `--remind` grouping at :2912

**Interfaces:**
- Consumes: `shift_day`, `shift_day_bounds` from Task 1.
- Produces: `_fetch_classified_day_entries(db, user_email, entry_date)` where `entry_date` is a shift day; signature unchanged.

- [ ] **Step 1: Write the failing test**

Append to `swift_api_pipeline/tests/test_shift_day.py`:

```python
def test_fetch_classified_day_entries_uses_shift_day_bounds():
    import timer_correction_review as tcr

    captured = {}

    class FakeDB:
        def fetch(self, sql, *params):
            captured["sql"] = sql
            captured["params"] = params
            return []

    tcr._fetch_classified_day_entries(FakeDB(), "a@ontel.co", date(2026, 9, 22))
    sql = " ".join(captured["sql"].split())
    assert "America/New_York" not in sql
    assert sql.count("start_time >= $2 AND") == 2      # surviving + removals
    assert sql.count("start_time < $3") == 2
    lo, hi = shift_day_bounds(date(2026, 9, 22))
    assert captured["params"] == ("a@ontel.co", lo, hi)
```

- [ ] **Step 2: Run the test to verify it fails**

Run: `cd swift_api_pipeline && python -m pytest tests/test_shift_day.py -q -p no:warnings -k classified`

Expected: FAIL, `America/New_York` still in the SQL.

- [ ] **Step 3: Rewrite the two predicates**

In `_fetch_classified_day_entries`, add right after the docstring (before `rows = retry_db(`):

```python
    lo, hi = shift_day_bounds(entry_date)
```

At :2319 replace

```sql
                  AND DATE(t.start_time AT TIME ZONE 'America/New_York') = $2
```

with

```sql
                  AND t.start_time >= $2 AND t.start_time < $3
```

At :2373 replace

```sql
              AND DATE(rm.start_time AT TIME ZONE 'America/New_York') = $2
```

with

```sql
              AND rm.start_time >= $2 AND rm.start_time < $3
```

And change the call's parameter list at :2377 from `""", user_email, entry_date),` to:

```python
        """, user_email, lo, hi),
```

The `description=f"classify entries for {user_email} on {entry_date}"` line stays.

- [ ] **Step 4: Fix the `--remind` grouping**

At :2909-2912 replace:

```python
        st = r["start_time"]
        if st.tzinfo is None:
            st = st.replace(tzinfo=timezone.utc)
        entry_date = st.astimezone(TZ_EASTERN).date()
```

with:

```python
        entry_date = shift_day(r["start_time"])
```

- [ ] **Step 5: Run the tests to verify they pass**

Run: `cd swift_api_pipeline && python -m pytest tests/test_shift_day.py -q -p no:warnings`

Expected: `22 passed`.

- [ ] **Step 6: Run the full suite**

Run: `cd swift_api_pipeline && python -m pytest -q -p no:warnings --ignore=tests/render_running_entries_sample.py 2>&1 | tail -3`

Expected: `6 failed, 336 passed`, same six. `test_confirmation_duplicates.py` and `test_duplicate_member_removals.py` exercise this area and must stay green.

- [ ] **Step 7: Commit**

```bash
git add swift_api_pipeline/timer_correction_review.py swift_api_pipeline/tests/test_shift_day.py
git commit -m "timer emails: --remind buckets on the shift day

_fetch_classified_day_entries uses start_time ranges on shift_day_bounds
for both the surviving and removals branches; reminder grouping uses
shift_day.

Co-Authored-By: Claude Fable 5.1 <noreply@anthropic.com>"
```

---

### Task 5: `--resend` current-day fetch and lookback cutoff

**Files:**
- Modify: `swift_api_pipeline/timer_correction_review.py`
  - `_fetch_current_day_entries` at :3111-3150
  - `find_days_needing_resend` cutoff at :3163

**Interfaces:**
- Consumes: `shift_day`, `shift_day_bounds` from Task 1.
- Produces: signatures unchanged; `send_date` rows in `daily_notifications` are read as shift days.

- [ ] **Step 1: Write the failing tests**

Append to `swift_api_pipeline/tests/test_shift_day.py`:

```python
def test_fetch_current_day_entries_uses_shift_day_bounds():
    import timer_correction_review as tcr

    captured = {}

    class FakeDB:
        def fetch(self, sql, *params):
            captured["sql"] = sql
            captured["params"] = params
            return []

    tcr._fetch_current_day_entries(FakeDB(), "a@ontel.co", date(2026, 9, 22))
    sql = " ".join(captured["sql"].split())
    assert "America/New_York" not in sql
    assert "c.start_time >= $2 AND c.start_time < $3" in sql
    lo, hi = shift_day_bounds(date(2026, 9, 22))
    assert captured["params"] == ("a@ontel.co", lo, hi)


def test_find_days_needing_resend_cutoff_is_shift_day_based():
    import timer_correction_review as tcr

    captured = {}

    class FakeDB:
        def fetch(self, sql, *params):
            captured["params"] = params
            return []

    tcr.find_days_needing_resend(FakeDB(), lookback_days=7)
    expect = shift_day(datetime.now(timezone.utc)) - timedelta(days=7)
    assert captured["params"] == (expect,)
```

- [ ] **Step 2: Run the tests to verify they fail**

Run: `cd swift_api_pipeline && python -m pytest tests/test_shift_day.py -q -p no:warnings -k "current_day or cutoff"`

Expected: 2 FAIL.

- [ ] **Step 3: Rewrite `_fetch_current_day_entries`**

Add after its docstring:

```python
    lo, hi = shift_day_bounds(entry_date)
```

At :3146 replace

```sql
              AND DATE(c.start_time AT TIME ZONE 'America/New_York') = $2
```

with

```sql
              AND c.start_time >= $2 AND c.start_time < $3
```

and the parameter list at :3148 from `""", user_email, entry_date),` to:

```python
        """, user_email, lo, hi),
```

- [ ] **Step 4: Fix the lookback cutoff**

At :3163 replace

```python
    cutoff = (datetime.now(TZ_EASTERN) - timedelta(days=lookback_days)).date()
```

with

```python
    cutoff = shift_day(datetime.now(timezone.utc)) - timedelta(days=lookback_days)
```

- [ ] **Step 5: Confirm no ET-date bucketing is left**

Run: `grep -n "AT TIME ZONE 'America/New_York')\s*=\|astimezone(TZ_EASTERN).date()" swift_api_pipeline/timer_correction_review.py`

Expected: no output. (`TZ_EASTERN` itself stays; it is still used for display formatting of times inside the email, which is correct and out of scope.)

- [ ] **Step 6: Run the tests to verify they pass**

Run: `cd swift_api_pipeline && python -m pytest tests/test_shift_day.py -q -p no:warnings`

Expected: `24 passed`.

- [ ] **Step 7: Run the full suite**

Run: `cd swift_api_pipeline && python -m pytest -q -p no:warnings --ignore=tests/render_running_entries_sample.py 2>&1 | tail -3`

Expected: `6 failed, 338 passed`, same six.

- [ ] **Step 8: Commit**

```bash
git add swift_api_pipeline/timer_correction_review.py swift_api_pipeline/tests/test_shift_day.py
git commit -m "timer emails: --resend reads send_date as a shift day

_fetch_current_day_entries uses a start_time range on shift_day_bounds;
the lookback cutoff is shift-day based. No ET-calendar-date bucketing
remains in the emails run.

Co-Authored-By: Claude Fable 5.1 <noreply@anthropic.com>"
```

---

### Task 6: Trigger, workflow comments, README

**Files:**
- Modify: `scripts/pipeline_trigger.gs:150-197`
- Modify: `.github/workflows/pipeline-timer-emails.yml:1-20` (header comment) and the `--send` step comment
- Modify: `.github/workflows/pipeline-timer.yml` the "Member-facing emails ... moved" comment block
- Modify: `README.md:35` and `README.md:108-118`

**Interfaces:** none (config and docs).

- [ ] **Step 1: Rewrite the trigger block in `pipeline_trigger.gs`**

Replace :150-163 (the comment plus the two `var` lines) with:

```js
/**
 * Target time for the daily Timer EMAILS run: ~06:30 Asia/Manila, just
 * after the members' 06:00 PHT shift end. Since 2026-09-28 the email is
 * bucketed on a SHIFT DAY (06:00 PHT to 06:00 PHT) and sent as soon as
 * that window closes; before that it fired ~6:00 AM ET (18:00 PHT, the
 * next shift's start) and covered the ET calendar date.
 *
 * Minute 30, not 0: Apps Script jitter is +/-15 min and the window MUST be
 * closed before the run, so 06:15-06:45 PHT rather than 05:45-06:15. The
 * trigger is pinned to Asia/Manila (see setupTimerEmailsTrigger) so it does
 * not drift an hour at US DST changes. Late stops that reach Swift after
 * the send are picked up in-thread by the next morning's --resend.
 *
 * This is run 2 of 2 for timer data: it re-extracts, applies corrections,
 * rebuilds the clean table, then sends the member-facing emails
 * (--remind / --send / --resend). Run 1 (triggerLightPipelines, ~1:15 AM
 * ET / 13:15 PHT) is data + Excel exports only since 2026-08-28.
 *
 * DIFFERENT dispatch type from 'pipeline-timer' on purpose: the two runs must
 * never be fired by the same trigger.
 */
var TIMER_EMAILS_HOUR = 6;     // Asia/Manila
var TIMER_EMAILS_MINUTE = 30;
```

Replace the `setupTimerEmailsTrigger` docblock and function at :169-197 with:

```js
/**
 * Idempotently (re)create the daily time-driven trigger for
 * triggerTimerEmails() at ~06:30 Asia/Manila. RUN THIS ONCE from the Apps
 * Script editor after deploying this file (and again after editing
 * TIMER_EMAILS_HOUR/MINUTE); it deletes any existing trigger on the same
 * handler first, so re-running is safe.
 *
 * CUTOVER (2026-09-28): run this only BETWEEN an old 18:00 PHT send and the
 * next 06:15 PHT, after the daily_notifications snapshot reset (spec 3.5).
 */
function setupTimerEmailsTrigger() {
  var existing = ScriptApp.getProjectTriggers();
  for (var i = 0; i < existing.length; i++) {
    if (existing[i].getHandlerFunction() === 'triggerTimerEmails') {
      ScriptApp.deleteTrigger(existing[i]);
    }
  }

  ScriptApp.newTrigger('triggerTimerEmails')
    .timeBased()
    .everyDays(1)
    .atHour(TIMER_EMAILS_HOUR)
    .nearMinute(TIMER_EMAILS_MINUTE)
    .inTimezone('Asia/Manila')
    .create();

  Logger.log('Created triggerTimerEmails trigger at ~' +
             TIMER_EMAILS_HOUR + ':' + (TIMER_EMAILS_MINUTE < 10 ? '0' : '') + TIMER_EMAILS_MINUTE +
             ' Asia/Manila daily (+/-15 min).');
}
```

Also search the file for the other mention at :795 (`Also EXCLUDES triggerTimerEmails`) and any `6:00 AM ET` / `18:00 PHT` text tied to timer emails; update wording to `~06:30 PHT` where it describes this trigger. Do not touch other triggers' times.

- [ ] **Step 2: Update `pipeline-timer-emails.yml`**

Replace the header comment (lines 3 to 20, everything between `name:` and `on:`) with:

```yaml
# Run 2 of 2 for timer data (~06:30 PHT, just after the members' 06:00 PHT
# shift end). Since 2026-09-28 the Timer Entries email is bucketed on a SHIFT
# DAY, 06:00 PHT to 06:00 PHT, labelled with the date the shift started, and
# is sent as soon as that window closes. (Before that it ran ~6:00 AM ET /
# 18:00 PHT and covered the ET calendar date, which is noon PHT to noon PHT
# and disagreed with the shift on the 06:00-12:00 PHT overtime band, ~10% of
# entries.) It re-extracts the month-to-date first, then runs the
# member-facing steps: duplicate reminders, the daily Timer Entries email and
# the threaded re-sends. Stops that reach Swift after the send are delivered
# in-thread by the next morning's --resend. No Excel exports here: those stay
# on pipeline-timer.yml (~1:15 AM ET / 13:15 PHT) because Sheena's report is
# built from the 13:15 PHT clean export.
#
# Dispatched by scripts/pipeline_trigger.gs -> triggerTimerEmails() via the
# `pipeline-timer-emails` repository_dispatch type. Deliberately a DIFFERENT
# event type from `pipeline-timer` so the two runs can never be fired by the
# same trigger.
#
# NOTE: `--send` upserts app_timer.daily_notifications ON CONFLICT DO UPDATE
# and will re-send the day if run twice. Do not add a retry loop around the
# email steps, and do not workflow_dispatch this after a successful run on
# the same day unless a second send is intended.
```

Update the `--send` step comment `# Target date = yesterday (ET) = the shift that ended ~12h ago.` to:

```yaml
        # Target = the shift day that just closed at 06:00 PHT (the shift
        # that started 18:00 PHT yesterday).
```

Update the `Second extract of the day. --email-on-failure-only: the 1:15 AM run already sends the routine success email` comment: it is still true (the 13:15 PHT run is later in the PHT day but it is the one that emails on success), so change only "Second extract of the day" to "First extract of the PHT day".

Update the corrections step comment `Applies any form responses that arrived since 1:15 AM` to `Applies any form responses that arrived since the 13:15 PHT run`.

- [ ] **Step 3: Update `pipeline-timer.yml`**

Replace the block:

```yaml
      # Member-facing emails (--remind / --send / --resend) moved to
      # pipeline-timer-emails.yml (~6:00 AM ET / 18:00 PHT, 2026-08-28). That
      # run re-extracts first so stops that reach Swift late (hours after the
      # shift, seen 3x in Aug 2026) are settled before anyone is emailed. This
      # run is data + exports only. Do NOT add the send steps back here.
```

with:

```yaml
      # Member-facing emails (--remind / --send / --resend) live in
      # pipeline-timer-emails.yml (~06:30 PHT since 2026-09-28, previously
      # ~6:00 AM ET / 18:00 PHT from 2026-08-28). This run is data + exports
      # only. Do NOT add the send steps back here.
```

- [ ] **Step 4: Update README**

Line 35, replace the `pipeline-timer-emails.yml` row with:

```markdown
| `pipeline-timer-emails.yml` | Apps Script ~06:30 PHT (`triggerTimerEmails`, pinned to Asia/Manila) | Run 2 of 2: re-extract + backfill + apply/rebuild + MV refresh, then the member-facing emails (`--remind`, `--send`, `--resend`) for the shift day that closed at 06:00 PHT. Separate `pipeline-timer-emails` dispatch type; same `pipeline-timer` concurrency group. No exports |
```

In the paragraph at lines 108 to 118, replace the sentence fragment

```
`pipeline-timer-emails.yml` (~6:00 AM ET / 18:00 PHT, members' shift start)
re-extracts first and then runs `--remind` / `--send` / `--resend`, giving late
stops ~12 hours to land.
```

with

```
`pipeline-timer-emails.yml` re-extracts first and then runs `--remind` /
`--send` / `--resend`. From 2026-08-28 to 2026-09-27 it ran ~6:00 AM ET /
18:00 PHT (members' shift start), giving late stops ~12 hours to land. Since
2026-09-28 it runs ~06:30 PHT, just after the 06:00 PHT shift end, and the
email is bucketed on a **shift day** (06:00 PHT to 06:00 PHT, labelled with
the date the shift started) instead of the ET calendar date; the ET date is
noon PHT to noon PHT and disagreed with the shift on the 06:00-12:00 PHT
overtime band (~10% of entries, 65 of 82 members over 30 days). Late stops
now arrive in-thread via the next morning's `--resend`. Only this run uses
the shift day; DRMC, variance and the Excel exports stay on ET calendar
dates. Design: `docs/superpowers/specs/2026-09-23-timer-emails-shift-day-design.md`.
```

- [ ] **Step 5: Check for stray references**

Run: `grep -rn "18:00 PHT\|6:00 AM ET\|shift start" README.md swift_api_pipeline/README.md .github/workflows/pipeline-timer*.yml scripts/pipeline_trigger.gs | grep -i "email"`

Expected: only lines that describe the *history* (2026-08-28 to 2026-09-27). Fix any that still describe the current behaviour.

- [ ] **Step 6: Run the full suite (no code changed, sanity)**

Run: `cd swift_api_pipeline && python -m pytest -q -p no:warnings --ignore=tests/render_running_entries_sample.py 2>&1 | tail -3`

Expected: `6 failed, 338 passed`.

- [ ] **Step 7: Commit**

```bash
git add scripts/pipeline_trigger.gs .github/workflows/pipeline-timer-emails.yml .github/workflows/pipeline-timer.yml README.md
git commit -m "timer emails: trigger at ~06:30 Asia/Manila, docs

TIMER_EMAILS_HOUR/MINUTE = 6:30, setupTimerEmailsTrigger pinned with
inTimezone('Asia/Manila'), nearMinute(30) so the 06:00 PHT window is closed
before the run. Workflow headers and README describe the shift day.

Co-Authored-By: Claude Fable 5.1 <noreply@anthropic.com>"
```

---

### Task 7: Verification against production data, dry run, WORK_LOG

**Files:**
- Create: `out/timer_emails_shift_day_check.sql` (kept for the cutover)
- Modify: `WORK_LOG.md` (append at the END)

**Interfaces:** none.

- [ ] **Step 1: Prove the SQL bounds match the Python definition on real rows**

Run this read-only query against production (Supabase MCP `execute_sql`, project `voqfjfngdpcvevbkikud`) and save it as `out/timer_emails_shift_day_check.sql`:

```sql
-- Shift-day parity check: the range predicate the code now issues must
-- agree with the closed-form expression for every row in the window.
WITH bounds AS (
  SELECT d::date AS shift_day,
         (d::date::timestamp + interval '6 hours') AT TIME ZONE 'Asia/Manila'          AS lo,
         ((d::date + 1)::timestamp + interval '6 hours') AT TIME ZONE 'Asia/Manila'    AS hi
  FROM generate_series(CURRENT_DATE - 8, CURRENT_DATE - 1, interval '1 day') d
)
SELECT b.shift_day,
       COUNT(*) FILTER (WHERE t.start_time >= b.lo AND t.start_time < b.hi)                          AS by_range,
       COUNT(*) FILTER (WHERE ((t.start_time AT TIME ZONE 'Asia/Manila') - interval '6 hours')::date
                                = b.shift_day)                                                     AS by_expr,
       COUNT(*) FILTER (WHERE DATE(t.start_time AT TIME ZONE 'America/New_York') = b.shift_day)    AS by_old_et_day
FROM bounds b
JOIN data_staging.stg_timer_activities_clean t
  ON t.start_time >= b.lo - interval '1 day' AND t.start_time < b.hi + interval '1 day'
GROUP BY b.shift_day
ORDER BY b.shift_day;
```

Expected: `by_range = by_expr` on every row. `by_old_et_day` differs (that is the 10% band). Also confirm in Python that `shift_day_bounds(d)` prints the same `lo`/`hi` instants as the SQL `bounds` CTE for one `d`:

```bash
cd swift_api_pipeline && python -c "from datetime import date; from timer_correction_review import shift_day_bounds; print(shift_day_bounds(date(2026,9,22)))"
```

Expected: `(datetime(2026, 9, 21, 22, 0, tzinfo=UTC), datetime(2026, 9, 22, 22, 0, tzinfo=UTC))`.

- [ ] **Step 2: Dry-run `--send` for one shift day to Jamil only**

The workflow creates `.env` and `gmail_credentials/` from secrets; locally they already exist in `swift_api_pipeline/` on the main checkout. This worktree is a fresh checkout, so copy them in (they are gitignored):

```bash
cp ../../../swift_api_pipeline/.env swift_api_pipeline/.env
cp -r ../../../swift_api_pipeline/gmail_credentials swift_api_pipeline/gmail_credentials
```

Then, with Cloudflare WARP on:

```bash
cd swift_api_pipeline && python -u timer_correction_review.py --send --test --date 2026-09-22 2>&1 | tail -20
```

Expected: log lines `Fetching timer entries for 2026-09-22`, `Found N entries for M techs`, and emails delivered to jamil.mendez@ontel.co only (that is what `--test` does). Open one and confirm: subject/header says "September 22, 2026", and it lists entries from 18:00 PHT 9/22 through the early morning of 9/23, including any that started 05:00 to 05:59 PHT 9/23 and **none** that started at or after 06:00 PHT 9/23.

Note `--send --test` still upserts `app_timer.daily_notifications` for the *real* member rows? Check `send_daily_emails`: if the upsert happens regardless of `test_mode`, run the dry run against a date already sent (9/22 was sent by the 18:00 PHT run on 9/23) so the only effect is a refreshed snapshot for that day, and record that in WORK_LOG. If it is gated on `not test_mode`, no side effect.

- [ ] **Step 3: Append the WORK_LOG entry**

Append at the END of `WORK_LOG.md` (never insert mid-file), using an ET time range for the session:

```markdown
## 2026-09-23 (8:50 PM to <end> ET) Timer Entries email: 06:00 PHT shift day (INIT-043)

Jamil: move the daily Timer Entries email from 18:00 PHT to 06:00 PHT, covering
06:00 PHT to 06:00 PHT. Data run (13:15 PHT) untouched.

Found before coding: the email had no PHT window at all; every bucketing site
used the ET calendar date (noon PHT to noon PHT). A trigger retime to 18:00 ET
targeting "today ET" would fire at the 75% mark of the ET day and drop the
06:00-12:00 PHT overtime band: 1,309 of 12,681 entries (10.3%), 65 of 82
members, 26 of 30 days. So the day is re-anchored instead.

Branch `feat/timer-emails-shift-day` (worktree). Spec
`docs/superpowers/specs/2026-09-23-timer-emails-shift-day-design.md`, plan
`docs/superpowers/plans/2026-09-23-timer-emails-shift-day.md`.

- `timer_correction_review.py`: `shift_day` / `shift_day_bounds` /
  `form_lookup_bounds` / `last_closed_shift_day`; `--send`, `--remind`,
  `--resend` and the `--apply` change records all bucket on the shift day;
  SQL moved from `DATE(start_time AT TIME ZONE ...) = $1` to sargable
  `start_time >= $lo AND start_time < $hi`. `_entry_date_et` removed.
- `pipeline_trigger.gs`: `TIMER_EMAILS_HOUR/MINUTE = 6:30`,
  `setupTimerEmailsTrigger` pinned `inTimezone('Asia/Manila')`.
- Tests: `tests/test_shift_day.py` (24). Suite 338 passed / 6 failed, the six
  pre-existing in `test_asset_tasks_resilience.py` on main `ed3671a`.
- Parity check vs production (`out/timer_emails_shift_day_check.sql`): range
  predicate == closed-form expression on every shift day checked.
- Dry run `--send --test --date 2026-09-22` to jamil.mendez@ontel.co: <result>.

CUTOVER CHECKLIST (Jamil, window = after an old 18:00 PHT send and before the
next 06:15 PHT; Sunday 9/27 evening PHT or Monday 9/28 evening PHT):
1. Merge the PR (safe any time before; nothing fires until the trigger moves).
2. Snapshot reset so --resend does not misread the definition change as new
   entries for up to ~65 members:
   `UPDATE app_timer.daily_notifications SET last_sent_entry_ids = NULL WHERE send_date >= CURRENT_DATE - 8;`
3. Paste `scripts/pipeline_trigger.gs` whole into the Apps Script editor, run
   `setupTimerEmailsTrigger()` once, confirm the Triggers page shows
   triggerTimerEmails at 6:30 AM Asia/Manila and no 6 AM ET entry.
4. Next morning: check the workflow run and one member email (date label,
   05:xx PHT entries present, 06:xx absent).

Known consequences (spec 3.7): overtime starting after 06:00 PHT lands in the
next shift's email; the email's day is 6 h offset from DRMC/variance/exports.
```

Fill in `<end>` and `<result>` with the real values.

- [ ] **Step 4: Commit**

```bash
git add out/timer_emails_shift_day_check.sql WORK_LOG.md
git commit -m "timer emails: parity check SQL, WORK_LOG with cutover checklist

Co-Authored-By: Claude Fable 5.1 <noreply@anthropic.com>"
```

- [ ] **Step 5: Push and open the PR**

```bash
git push -u origin feat/timer-emails-shift-day
gh pr create --title "Timer Entries email: 06:30 PHT on a 06:00 PHT shift day (INIT-043)" --body-file - <<'EOF'
Moves the member-facing Timer Entries email from ~18:00 PHT to ~06:30 PHT and buckets it on a shift day (06:00 PHT to 06:00 PHT, labelled with the date the shift started) instead of the ET calendar date.

Why not just retime the trigger: the ET calendar date is noon PHT to noon PHT. Firing at 18:00 ET and targeting "today ET" would send at the 75% mark and drop the 06:00-12:00 PHT overtime band: 1,309 of 12,681 entries (10.3%), 65 of 82 members, 26 of 30 days (measured 2026-09-23).

Spec: `docs/superpowers/specs/2026-09-23-timer-emails-shift-day-design.md`

- `timer_correction_review.py`: `shift_day` / `shift_day_bounds` / `form_lookup_bounds` / `last_closed_shift_day`; `--send`, `--remind`, `--resend` and the `--apply` change records bucket on the shift day; SQL is now a sargable `start_time` range.
- `pipeline_trigger.gs`: 06:30 Asia/Manila, pinned time zone.
- Workflow headers, README, WORK_LOG with the cutover checklist.
- Tests: `tests/test_shift_day.py` (24). Suite 338 passed / 6 failed (the six pre-existing `test_asset_tasks_resilience` failures on main).

**Cutover is manual and time-boxed** (WORK_LOG checklist): after an old 18:00 PHT send and before the next 06:15 PHT, run the `daily_notifications` snapshot reset, then paste the .gs and run `setupTimerEmailsTrigger()` once. Merging early is safe.

Scope: emails run only. `pipeline-timer.yml` (13:15 PHT data + exports), DRMC, variance, exports and MVs are unchanged; their day stays the ET calendar date.

🤖 Generated with [Claude Code](https://claude.com/claude-code)
EOF
```

Then invoke the `premerge-review` skill before any merge. Merge is Jamil's call and is not part of this plan.
