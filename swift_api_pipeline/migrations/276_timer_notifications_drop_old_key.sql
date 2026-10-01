-- ═══════════════════════════════════════════════════════════════════════════════
-- 276_timer_notifications_drop_old_key.sql  (step 2 of 2; step 1 = 275)
-- Drop the old UNIQUE (user_email, send_date) on app_timer.daily_notifications
-- now that the two-window code keys rows by (user_email, send_date, send_window).
--
-- Apply right AFTER the two-window PR is merged (its INSERT targets the new
-- 3-column key, which 275 already created) and BEFORE the first 'second'-window
-- send: the first (member, date) that gets both a 'full'/'first' row and a
-- 'second' row would violate this old key. Until this runs, the merged code
-- still works for 'first' sends on dates with no earlier row.
--
-- The 275 header carries the live preflight. The old key survived migration 117
-- under its original stg_ name.
-- ═══════════════════════════════════════════════════════════════════════════════
BEGIN;
SET LOCAL lock_timeout = '5s';

ALTER TABLE app_timer.daily_notifications
    DROP CONSTRAINT IF EXISTS stg_timer_daily_notifications_user_email_send_date_key;
ALTER TABLE app_timer.daily_notifications
    DROP CONSTRAINT IF EXISTS daily_notifications_user_email_send_date_key;

COMMIT;

-- Verify:
--   SELECT conname FROM pg_constraint WHERE conrelid = 'app_timer.daily_notifications'::regclass;
--     -> pkey, daily_notifications_send_window_check,
--        daily_notifications_user_email_send_date_send_window_key   (no 2-column key)
