-- ═══════════════════════════════════════════════════════════════════════════════
-- 275_timer_notifications_window.sql  (step 1 of 2; step 2 = 276)
-- Timer Entries email: two windows per Eastern day (first = 12 AM to 6 PM ET,
-- second = 6 PM to 12 AM ET). One notification row per (member, Eastern date,
-- window). Rows written before this change covered a whole day (ET day before
-- 2026-09-28, 06:00 PHT shift day after) and are marked 'full'.
--
-- Column is `send_window`, not `window`: `window` is a fully reserved word in
-- Postgres (pg_get_keywords catcode R) and the standard forbids quoted
-- identifiers.
--
-- ADDITIVE ONLY, safe to apply BEFORE the two-window code is merged: the old
-- code's INSERT does not name the new column (rows default to 'full') and its
-- ON CONFLICT (user_email, send_date) still has the old unique key. Migration
-- 276 drops that old key and must run right AFTER the merge, before the first
-- 'second'-window send (a second row for a (member, date) that already has a
-- 'full' or 'first' row would violate the old key).
--
-- Spec: docs/superpowers/specs/2026-10-01-timer-email-two-windows-design.md
--
-- Live preflight (run 2026-10-01 ~08:40 ET via the IPv4 session pooler; DB
-- preflight lane re-checked ~09:30 ET):
--   rows: 10,547 (4.9 MB); send_date 2026-03-18 .. 2026-09-30
--   constraints before: stg_timer_daily_notifications_pkey,
--                       stg_timer_daily_notifications_user_email_send_date_key
--   indexes before:     the two above + idx_stg_timer_daily_notifs_last_sent
--   dependents (pg_depend via pg_rewrite): none; functions referencing it: none
--   (user_email, send_date) pairs with more than one row: 0
--   ADD COLUMN ... NOT NULL DEFAULT 'full' is metadata-only on PG 15 (no rewrite).
-- ═══════════════════════════════════════════════════════════════════════════════
BEGIN;
SET LOCAL lock_timeout = '5s';

ALTER TABLE app_timer.daily_notifications
    ADD COLUMN IF NOT EXISTS send_window text NOT NULL DEFAULT 'full';

ALTER TABLE app_timer.daily_notifications
    DROP CONSTRAINT IF EXISTS daily_notifications_send_window_check;
ALTER TABLE app_timer.daily_notifications
    ADD CONSTRAINT daily_notifications_send_window_check
    CHECK (send_window IN ('full', 'first', 'second'));

ALTER TABLE app_timer.daily_notifications
    DROP CONSTRAINT IF EXISTS daily_notifications_user_email_send_date_send_window_key;
ALTER TABLE app_timer.daily_notifications
    ADD CONSTRAINT daily_notifications_user_email_send_date_send_window_key
    UNIQUE (user_email, send_date, send_window);

COMMENT ON COLUMN app_timer.daily_notifications.send_window IS
    'Email window: first = 12 AM to 6 PM ET, second = 6 PM to 12 AM ET (since 2026-10-01); full = whole-day sends before that.';

COMMIT;

-- Verify:
--   SELECT conname FROM pg_constraint WHERE conrelid = 'app_timer.daily_notifications'::regclass;
--     -> pkey, the OLD 2-column key (dropped by 276), the check, the new 3-column key
--   SELECT send_window, count(*) FROM app_timer.daily_notifications GROUP BY 1;   -- only 'full'
