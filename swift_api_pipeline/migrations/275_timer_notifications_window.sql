-- ═══════════════════════════════════════════════════════════════════════════════
-- 275_timer_notifications_window.sql
-- Timer Entries email: two windows per Eastern day (first = 12 AM to 6 PM ET,
-- second = 6 PM to 12 AM ET). One notification row per (member, Eastern date,
-- window). Rows written before this change covered a whole day (ET day before
-- 2026-09-28, 06:00 PHT shift day after) and are marked 'full'.
--
-- Spec: docs/superpowers/specs/2026-10-01-timer-email-two-windows-design.md
-- Preflight: pg_depend shows no view/MV/function depending on this table
-- (migration 117 notes; re-checked on the live DB before apply, result pasted
-- below). The only writer is timer_correction_review.py; its ON CONFLICT target
-- changes in the same PR, so apply this immediately AFTER that PR is merged,
-- never before (the old code's ON CONFLICT (user_email, send_date) needs the
-- old key; the new code's needs the new one).
--
-- Live preflight (fill in at apply time):
--   constraints before: <paste>
--   dependents (pg_depend via pg_rewrite): <paste, expected none>
-- ═══════════════════════════════════════════════════════════════════════════════
BEGIN;

ALTER TABLE app_timer.daily_notifications
    ADD COLUMN IF NOT EXISTS window text NOT NULL DEFAULT 'full';

ALTER TABLE app_timer.daily_notifications
    DROP CONSTRAINT IF EXISTS daily_notifications_window_check;
ALTER TABLE app_timer.daily_notifications
    ADD CONSTRAINT daily_notifications_window_check
    CHECK (window IN ('full', 'first', 'second'));

-- The old key may survive the 117 rename under its original stg_ name.
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

-- Verify:
--   SELECT conname FROM pg_constraint WHERE conrelid = 'app_timer.daily_notifications'::regclass;
--   SELECT window, count(*) FROM app_timer.daily_notifications GROUP BY 1;   -- only 'full' right after apply
