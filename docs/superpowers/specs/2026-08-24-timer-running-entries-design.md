# Timer Activity Entries: still-running timers (design, 2026-08-24)

## Problem
The nightly "Timer Activity Entries" email lists every raw row for yesterday,
including timers that are still running (`end_time IS NULL`, 0 min), each with
Edit / Remove buttons. A Remove on a running row is stored in
`app_timer.entry_removals` with `end_time = NULL`. `rebuild_timer_clean()`
matches removals on the exact natural key (end_time + duration_min), so once
the timer stops the row no longer matches, reappears in clean, and the resend
pass emails it back as NEW. Members read the remove as done and skip the
second email. Live audit 2026-08-24: 26 NULL-end removals stored, 20 of them
with the start-key still present in clean (124.2h).

Second, quieter defect: Step 5 of the rebuild (migration 197, drop untracked
same-start runaway duplicates >720 min when a <=720 sibling exists) treats ANY
`entry_removals` row on the start-key as "tracked" and skips the group. A
removal captured against a still-running snapshot therefore shields the later
runaway completions. Two prince rows (2026-07-29: 22.9h; 2026-08-19: 21.9h x2,
66.7h total) survive in clean for exactly this reason.

## Decision
1. **Running timers are not actionable in the email.** Rows with
   `end_time IS NULL` are excluded from the Daily Task Summary, the Entry
   Details table, and the resend snapshot. Editing or removing a duration
   that does not exist yet is the wrong affordance; the member's real action
   is to stop the timer in Swift.
2. **A "timer still running" notice replaces them.** One amber callout above
   the table listing each running timer (site / task, started hh:mm ET,
   elapsed as of send), a plain "Open Swift" link, and the sentence that the
   completed entry will arrive on this same thread with Edit / Remove. No
   buttons. Swift has no verified deep link for a timer entry (only
   `/#/app/assets/tasks/{task_did}/requirements` is proven, and timer rows
   carry `asset_did` not `task_did`), so the link goes to the Swift root.
   Deferred: add `task_did` to the timer extract and deep-link the notice.
3. **Ghost rows are dropped silently.** A NULL-end row whose start-key has a
   completed sibling in the same day is a stale extract snapshot (15 exist in
   raw today), not a running timer; it is neither listed nor noticed.
4. **Resend semantics unchanged.** When the timer completes its stable key
   changes (NULL -> set), it is absent from the snapshot, and the existing
   resend pass sends the threaded UPDATED email with the NEW badge and live
   buttons. `find_days_needing_resend` and the post-send snapshot writes now
   use the same settled-only set so a still-running timer can never trigger
   a resend by itself.
5. **Migration 241:** Step 5's `entry_removals` anti-join gains
   `AND rm.end_time IS NOT NULL`. A removal recorded before the entry had a
   duration is not a member decision about the completed rows. Expected
   effect on apply: the three prince runaway rows above leave clean; no other
   row changes (verified by the row-level preflight in this session).
6. **NOT doing drift-tolerant removal matching** (start-key-only match when
   the stored removal has NULL end_time). The row-level preflight showed it
   would be wrong more often than right: `audit_snapshot_cleanup` removals
   deliberately target only the stale 0-min row (fatima 1.8h, cris 1.3h would
   be deleted), the "0.0 ghost + completed row" pattern is the same (prince
   2.1h/0.1h/4.0h, noemi 2.0h), and corrections already win where the member
   edited (van, mikaela, luigi). The two genuinely ambiguous leftovers
   (gennell 2026-04-29 4.1h, randolf 2026-08-05 10.1h) are reported to Jamil,
   not auto-removed.

## Code
`swift_api_pipeline/timer_correction_review.py`
- `_split_running_entries(entries) -> (settled, running)`; ghost filtering and
  start-key dedupe live here.
- `_build_running_notice_html(running, now=None) -> str`.
- `send_daily_emails`, `send_resend_emails`, `find_days_needing_resend` use
  the split; `detect_and_track_duplicates` keeps the full list (it already
  skips NULL-end rows).
Tests: `swift_api_pipeline/tests/test_running_entries.py`.
