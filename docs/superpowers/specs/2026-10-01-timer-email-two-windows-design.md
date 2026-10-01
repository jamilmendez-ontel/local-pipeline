# Timer Entries email: two windows per Eastern day

Date: 2026-10-01. Owner: Jamil Mendez. Status: approved in chat, building.
Supersedes the single shift-day send of 2026-09-28 (`2026-09-23-timer-emails-shift-day-design.md`)
and absorbs PR #88 (the "What this email covers" callout), which is closed unmerged.

## 1. Why

Since 2026-09-28 each member gets one email per shift day (06:00 PHT to 06:00 PHT) at 06:30 PHT.
Entries from the 06:00 to 12:00 PHT morning band land in the next day's email labelled as the
next day, so members cannot find them where they expect, and Swift and DRMC label days by the
Eastern calendar date anyway. Jamil's decision on 2026-10-01: split the Eastern calendar day into
two emails and label both with that Eastern date.

| Email | Covers (ET) | Sent (ET) | Same moment in PHT |
|---|---|---|---|
| first | 12:00 AM to 6:00 PM on day D | 6:30 PM on D | 06:30 D+1 in summer, 07:30 D+1 in winter |
| second | 6:00 PM on D to 12:00 AM on D+1 | 9:00 AM on D+1 | 21:00 D+1 in summer, 22:00 D+1 in winter |

Boundaries are pinned to the Eastern clock and follow US DST (Jamil's choice, so the email day
equals DRMC's day all year).

## 2. Window model

A window is the pair `(day: date, part: 'first' | 'second')`, `day` being the Eastern date.

- `window_bounds(day, part) -> (lo_utc, hi_utc)`: half-open, built from `America/New_York`
  wall-clock times 00:00, 18:00 and 24:00, converted to UTC. Queries stay `start_time >= lo AND
  start_time < hi`.
- `window_of(instant) -> (day, part)`: the Eastern date of the instant; part is `first` when the
  Eastern hour is below 18, else `second`.
- `last_closed_window(now) -> (day, part)`: the most recent window whose `hi` is at or before
  `now`. At 6:30 PM ET on D this is `(D, first)`; at 9 AM ET on D+1 it is `(D, second)`; a late
  7:30 PM run still picks `(D, first)`.
- `form_lookup_bounds(day)`: the union of both windows of `day` and the legacy shift-day span
  (06:00 PHT `day` to 06:00 PHT `day`+1), that is `[min(00:00 ET day, 06:00 PHT day),
  00:00 ET day+1)`. This is the 30-hour span the code already uses, so replies in flight keep
  resolving.
- The legacy helpers `shift_day`, `shift_day_bounds`, `last_closed_shift_day` are removed from
  callers. `shift_day_bounds` survives only as `legacy_full_bounds(day)` for notification rows
  with `window = 'full'` (see 4).

## 3. CLI and workflow

`timer_correction_review.py`:

- `--send` targets `last_closed_window()` unless `--date YYYY-MM-DD` and `--window first|second`
  are both given (backfill). Giving one without the other is an error.
- `--remind`, `--resend` and `--apply` are unchanged in invocation; their internals use windows.

`.github/workflows/pipeline-timer-emails.yml` gains `workflow_dispatch` inputs `date` and
`window` (both optional, both or neither) passed to `--send`. The two Apps Script triggers keep
dispatching the bare `pipeline-timer-emails` event; the code picks the window by the clock.

## 4. Data: migrations 275 and 276

`app_timer.daily_notifications`. The column is `send_window`, not `window`: `window` is a fully
reserved word in Postgres and the standard forbids quoted identifiers (found by the DB preflight
lane on 2026-10-01).

- 275 (additive, apply BEFORE the merge): add `send_window text not null default 'full' check
  (send_window in ('full','first','second'))`; add unique `(user_email, send_date, send_window)`;
  keep the old `(user_email, send_date)` key so the old code keeps working until the merge.
- 276 (apply right AFTER the merge, before the first `second` send): drop the old
  `(user_email, send_date)` key.
- existing rows stay `full` (every send before this change covered a whole day: ET day before
  2026-09-28, shift day after). No backfill.
- Rollback files for both; valid while at most one row per user and date exists.
- Preflight (live, 2026-10-01): 10,547 rows, no pg_depend dependents, no functions referencing
  the table, 0 duplicate (user_email, send_date) pairs, old key name
  `stg_timer_daily_notifications_user_email_send_date_key`.

Send writes `send_window = part`. The insert's `ON CONFLICT` targets the new three-column key, so a
re-run of the same window re-sends and overwrites the thread ids, as today. Confirmation and
reminder thread lookups prefer the row for the exact window and fall back to a `full` row for the
same label date, so replies to pre-change emails keep threading during the transition.

## 5. Email content

- Subject: `Timer Activity Entries - {Month D, YYYY} (12 AM to 6 PM ET)` for `first`,
  `(6 PM to 12 AM ET)` for `second`. The date is the Eastern date `day`.
- Callout, first line of the body, both parts, both zones, derived from the bounds so DST is
  automatic:
  - first: "What this email covers: {Weekday, Month D} (Eastern date), 12:00 AM to 6:00 PM
    Eastern, which is {12:00 PM Mon D} to {6:00 AM Mon D+1} Philippine time. Entries started
    after 6:00 PM Eastern will be in the evening email, sent at 9 AM Eastern / 9 PM Philippine
    time."
  - second: "What this email covers: {Weekday, Month D} (Eastern date), 6:00 PM to 12:00 AM
    Eastern, which is {6:00 AM Mon D+1} to {12:00 PM Mon D+1} Philippine time. Your entries
    from 12 AM to 6 PM Eastern were in the earlier email."
  - PHT clock readings come from the converted bounds, so winter shows 7:00 AM and 1:00 PM.
- Intro sentence keeps "from {date}" with the Eastern date.
- Only members with at least one entry in the window are emailed (unchanged behaviour of
  `send_daily_emails`, which groups the fetched entries by member).
- The correction-confirmation and resend emails carry the same callout for their window.

## 6. Replies, confirmations, duplicates, resend

- Form replies name the label date; resolution uses `form_lookup_bounds(day)` plus the existing
  site/task/start match. Unchanged contract for members.
- `--apply` confirmation emails: the entry's window is `window_of(start_time)`; the confirmation
  shows that window's entries and threads to that window's notification row when one exists.
- Duplicate detection and reminders: `entry_date` becomes the Eastern date of the entry; the
  classified-day fetch takes the window of the group's first entry.
- `find_days_needing_resend`: iterate notification rows in the lookback; for each row use
  `window_bounds(send_date, window)` when `window` is `first` or `second`, and
  `legacy_full_bounds(send_date)` when `full`. Snapshot comparison and threading are per row.
  The lookback cutoff is `window_of(now).day - lookback_days`.

## 7. Scheduling (Apps Script, nanoninth `pipeline_trigger.gs`)

- `triggerTimerEmails` keeps its name and dispatch; its trigger moves to 18:30
  `America/New_York` (constants `TIMER_EMAILS_FIRST_HOUR = 18`, minute 30). Required: in winter
  06:30 PHT is 5:30 PM ET, before the first window closes, and the run would pick the wrong
  window.
- New `triggerTimerEmailsSecond` dispatching the same event, trigger at 09:00
  `America/New_York` (`TIMER_EMAILS_SECOND_HOUR = 9`, minute 0; Apps Script jitter of +/-15 min
  keeps it after the midnight close).
- `setupTimerEmailsTrigger()` deletes any trigger on either handler and recreates both. Jamil
  pastes the whole file into the nanoninth project and runs it once before 6:30 PM ET on
  2026-10-01.
- The emails workflow therefore runs twice a day (timer reload, corrections apply, clean rebuild,
  MV refresh, remind, send, resend). The extra rebuild is ~2.3 MB of WAL since PR #68. The
  concurrency group with `pipeline-timer` and `timer-correction-apply` is unchanged.

## 8. Cutover on 2026-10-01

1. Build on `feat/timer-email-two-windows` with tests first; premerge-review.
2. Apply migration 275 (additive) to OntelDB, then squash-merge, then apply 276 (drop the old
   key). The old code tolerates 275; the new code needs 276 before any `second` send.
3. Backfill: `gh workflow run pipeline-timer-emails.yml -f date=2026-09-30 -f window=second`,
   covering Sep 30 6 PM ET to Oct 1 12 AM ET. The 06:30 PHT catch-up already sent earlier today
   (row `window = full`, send_date 2026-09-30) covered Sep 29 6 PM ET to Sep 30 6 PM ET.
4. Jamil deploys the trigger file and runs `setupTimerEmailsTrigger()`.
5. Tonight 6:30 PM ET: `(2026-10-01, first)`. Tomorrow 9 AM ET: `(2026-10-01, second)`.

## 9. Tests

`tests/test_email_windows.py` replaces `tests/test_shift_day.py`:

- `window_bounds` summer and winter (UTC offsets 4 and 5), half-open, 18 h and 6 h long;
- `window_of` at 17:59:59 and 18:00:00 ET, and across the UTC midnight;
- `last_closed_window` at 6:30 PM ET, 9:00 AM ET, 7:30 PM ET (late winter run), 5:30 PM ET
  (must return the previous day's second window);
- `form_lookup_bounds` union in summer and winter;
- subject and callout text for both parts, including the winter PHT readings;
- `get_previous_day_entries` passes window bounds; `--date` without `--window` is rejected;
- notification insert carries `window`; `find_days_needing_resend` uses per-row bounds and the
  legacy bounds for `full` rows;
- migration 275 and its rollback are plain SQL, checked by the DB preflight, not by pytest.

## 10. Out of scope

- DRMC, variance, exports: already on Eastern calendar dates; untouched.
- The 1:15 AM ET data-only timer run: untouched.
- Any change to the Remove and Edit forms.
