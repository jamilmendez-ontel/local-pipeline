# Timer Entries email on a 06:00 PHT shift day

**Date:** 2026-09-23
**Status:** approved by Jamil 2026-09-23 (design in chat), implementation targeted for Monday 2026-09-28
**Scope:** `pipeline-timer-emails.yml` run only (the member-facing Timer Entries email and its `--remind` / `--resend` companions). The 13:15 PHT data + Excel exports run (`pipeline-timer.yml`) is untouched. DRMC, variance, exports and MVs are untouched.
**OnPoint:** subtask under INIT-043 Timer Activities System.

## 1. Request

Move the daily Timer Entries email from 18:00 PHT to 06:00 PHT, covering 06:00 PHT of the previous day to 06:00 PHT of the send day. Example: the email sent 06:00 PHT 9/23 covers 06:00 PHT 9/22 to 06:00 PHT 9/23.

## 2. Why this is not a trigger retime

The email run does not use a rolling window. Every date-bucketing site in `timer_correction_review.py` uses the **ET calendar date**:

```sql
DATE(start_time AT TIME ZONE 'America/New_York') = $1
```

An ET calendar day is 00:00 to 24:00 ET, which is **12:00 PHT to 12:00 PHT** (13:00 to 13:00 during US standard time). The requested window sits six hours earlier than that. The two definitions disagree on entries that start between 06:00 and 12:00 PHT.

Measured on `data_staging.stg_timer_activities_clean`, 2026-08-24 to 2026-09-23 (30 days):

| | value |
|---|---|
| entries starting 06:00 to 12:00 PHT | 1,309 of 12,681 (**10.3%**) |
| members with at least one such entry | 65 of 82 |
| days with at least one such entry | 26 of 30 |

Consequence: if the trigger simply moved to 18:00 ET (= 06:00 PHT) and targeted "today ET", the run would fire at the 75% mark of the ET day and the remaining 18:00 to 24:00 ET (= 06:00 to 12:00 PHT next morning) would not exist yet. Roughly 10% of entries, from most of the team, would be missing from their email every day. The trigger constant is the easy part; the bucketing has to change.

Rejected alternatives:

- **Trigger at 18:00 ET, target today ET.** Cuts the overtime band as above.
- **Trigger at 00:00 ET (noon PHT), target yesterday ET.** Zero code and a fully closed ET day, but that is a noon email, not 06:00.

## 3. Design

### 3.1 The shift-day primitive

A shift day is the 24 hours from 06:00 Asia/Manila on date D to 06:00 Asia/Manila on D+1, labelled **D** (the date the window opens, which is also the date the 18:00 PHT shift starts). Asia/Manila has no DST so the boundary is stable year-round.

Two helpers in `timer_correction_review.py`, next to the existing `TZ_EASTERN` block:

```python
TZ_MANILA = ZoneInfo("Asia/Manila")
SHIFT_DAY_START_HOUR = 6   # 06:00 PHT

def shift_day(dt) -> date:
    """Shift day an instant belongs to: the Asia/Manila date of (dt - 6h)."""

def shift_day_bounds(day: date) -> tuple[datetime, datetime]:
    """[06:00 PHT day, 06:00 PHT day+1) as tz-aware UTC datetimes."""
```

Boundary semantics: an entry starting 05:59:59 PHT 9/23 belongs to shift day **9/22**; one starting 06:00:00 PHT 9/23 belongs to **9/23**. Naive inputs are treated as UTC, matching every other datetime helper in the file.

SQL sites switch from `DATE(start_time AT TIME ZONE 'America/New_York') = $1` to

```sql
start_time >= $lo AND start_time < $hi
```

with `(lo, hi) = shift_day_bounds(day)`. This is also a strict improvement: the range predicate is sargable on the indexed `start_time`, the `DATE(...)` form was not.

### 3.2 Sites that change (`swift_api_pipeline/timer_correction_review.py`)

Line numbers are as of `ed3671a`.

| line | function / role | change |
|---|---|---|
| 353 | `_entry_date_et` (used at 1872, 1911, 1981, 2016 for duplicate-group dates) | replaced by `shift_day` |
| 656 to 670 | `get_previous_day_entries`, the `--send` query | shift-day bounds |
| 1021 to 1024 | `send_daily_emails` default target | last **closed** shift day: `shift_day(now) - 1 day` |
| 1440 to 1456 | form-response group lookup (`day_start` / `day_end`) | bounds widened to the union of both definitions, `[06:00 PHT D, 12:00 PHT D+1)`, so pending replies to pre-cutover emails still resolve; the lookup also matches on site/task/start so the extra six hours do not introduce ambiguity |
| 2319 | `--remind` classification, surviving entries | shift-day bounds |
| 2373 | `--remind` classification, removals | shift-day bounds |
| 2912 | `--remind` per-(user, date) grouping | `shift_day` |
| 3146 | `--resend` current-day entries | shift-day bounds |
| 3163 | `--resend` lookback cutoff | `shift_day(now) - lookback_days` |
| 656 / 1293 / 3518 | `--date` CLI argument and `target_date` plumbing | unchanged in shape; the date now means a shift day |

Everything else in the file (overlap clustering, duplicate detection, correction matching, HTML rendering, Gmail threading) is date-agnostic and does not change.

### 3.3 Email label and `daily_notifications.send_date`

The subject and header keep the existing `"%B %d, %Y"` format with the shift-day date, e.g. "September 22, 2026" for the email sent 06:30 PHT 9/23. That is the same label the old run would have put on the same shift, so `app_timer.daily_notifications.send_date` keeps meaning "the date in the email header" and the `(user_email, send_date)` key stays continuous across cutover. No migration.

### 3.4 Trigger (`scripts/pipeline_trigger.gs`)

`setupTimerEmailsTrigger()` follows the existing `setupPackageScrapeTrigger()` pattern and pins the time zone:

```js
var TIMER_EMAILS_HOUR = 6;     // 06:00 Asia/Manila
var TIMER_EMAILS_MINUTE = 30;
...
  .everyDays(1)
  .atHour(TIMER_EMAILS_HOUR)
  .nearMinute(TIMER_EMAILS_MINUTE)
  .inTimezone('Asia/Manila')
```

- `inTimezone('Asia/Manila')` so the send does not drift an hour at US DST changes (the old trigger accepted that drift; the new window is defined in PHT so the trigger should be too).
- `nearMinute(30)`: Apps Script jitter is plus or minus 15 minutes and the window must be **closed** before the run, so 06:15 to 06:45 PHT rather than 05:45 to 06:15. This 15 to 45 minutes is also the only late-stop buffer (see 3.6).

Comment blocks on `TIMER_EMAILS_HOUR` and `setupTimerEmailsTrigger()` are rewritten. The `pipeline-timer-emails.yml` header comment and the "Member-facing emails moved to ..." note in `pipeline-timer.yml` are updated to the new time and window. Deploy is the existing rule: Jamil pastes the whole file into the Apps Script editor and runs `setupTimerEmailsTrigger()` once; it deletes the old trigger on the same handler first.

### 3.5 Cutover

Two hazards, both handled.

**a. Spurious threaded re-sends.** `find_days_needing_resend` compares each `(user, send_date)` row in the 7-day lookback against its stored `last_sent_entry_ids` snapshot and re-sends when the current set has new IDs. Every stored snapshot was taken under the ET-day definition. Under the shift-day definition every weekday in the lookback *gains* its 06:00 to 12:00 PHT band (the previous night's overtime), so the first new run would fire threaded re-sends to up to ~65 members for up to 7 days each.

Fix: a one-off SQL in the cutover window, before the first new run:

```sql
UPDATE app_timer.daily_notifications
   SET last_sent_entry_ids = NULL
 WHERE send_date >= CURRENT_DATE - 8;
```

The code's existing bootstrap path (rows with NULL `last_sent_entry_ids` are re-snapshotted silently and not returned as candidates) then absorbs the definition change without sending anything. Cost, accepted: a genuine late entry on one of those 7 days is absorbed silently, once.

**b. Gap or double-send.** Old send: 06:00 ET = 18:00 PHT, target yesterday ET. New send: 06:30 PHT, target last closed shift day. The swap must happen **between an old 18:00 PHT send and the next 06:15 PHT**. Two clean options; Jamil picks because he does the paste:

- **Sunday 9/27 after 18:00 PHT.** First new email Monday 9/28 06:30 PHT covering shift day 9/27 (Sunday, near-empty); Tuesday 06:30 covers Monday's shift. "Implemented Monday" literally.
- **Monday 9/28 after 18:00 PHT.** First new email Tuesday 9/29 06:30 PHT covering shift day 9/28. Monday's 06:00 to 12:00 PHT band appears in both Monday's old email (as ET Sunday) and Tuesday's new one, once.

Order inside the window: (1) snapshot SQL, (2) paste `pipeline_trigger.gs` + run `setupTimerEmailsTrigger()`. The code PR merges any time before the window; merging early is safe because nothing fires until the trigger moves.

### 3.6 Late stops

The 2026-08-28 move to 18:00 PHT existed so stops that reach Swift hours after the shift are settled before anyone is emailed (3 cases in Aug 2026, e.g. an 18:13 ET stop that appeared after 01:27 ET). Sending at 06:30 PHT gives up most of that. Decision: accept it and lean on `--resend`, which already threads a follow-up when a prior day's entry set gains IDs, so a late stop is delivered in-thread by the next morning's run. The 15 to 45 minute `nearMinute(30)` offset is the only buffer; a buffer large enough to catch the Aug cases would push the email into the members' sleep and defeat the request.

### 3.7 Known consequences, stated once

- Overtime that **starts** after 06:00 PHT lands in the *next* shift's email. That is inherent in a 06:00-to-06:00 window.
- The email's day is now six hours offset from DRMC, variance, Hours Analysis and the Excel exports, which all stay on ET calendar dates. The two will disagree for entries in the 06:00 to 12:00 PHT band (~10%). Jamil chose email-only scope; the header is not relabelled. Relabelling ("Shift ending 06:00 PHT Sep 23") is a one-line follow-up if members ask.
- The 13:15 PHT data run still lands **after** the 06:30 PHT email run, so Sheena's clean export continues to see corrections applied by the email run.

## 4. Testing

- Unit tests (new `tests/test_shift_day.py`): the 05:59:59 vs 06:00:00 PHT boundary; ET summer and winter inputs; UTC-naive inputs; `shift_day_bounds` round-trips through `shift_day`; the widened form-lookup bounds contain both the old ET-day and the new shift-day windows.
- SQL/Python parity: for a sample of real `start_time` values, `shift_day()` agrees with the range predicate produced by `shift_day_bounds()`.
- Existing suite stays at the baseline: 314 passed, 6 failed, all six in `tests/test_asset_tasks_resilience.py` (pre-existing on main `ed3671a`, unrelated).
- Dry runs to jamil.mendez@ontel.co only: `--send --test --date 2026-09-22`, compared against the same day's current output; `--resend` in test mode after the snapshot reset on a copy of the lookback.
- `premerge-review` before merge. README, CHANGELOG, workflow comments in the same PR.

## 5. Out of scope

- Any change to `pipeline-timer.yml`, the Excel exports, DRMC, variance, MVs or `reference.*`.
- A platform-wide shift-day definition.
- Relabelling the email header.
- A second same-day catch-up run.
