-- 275_timer_notifications_window_ROLLBACK.sql
-- Reverts migration 275 (additive step). Run 276_..._ROLLBACK.sql FIRST if 276
-- was applied, so the old 2-column key exists again before the column goes.
-- Precondition: no (user_email, send_date) has more than one row, i.e. no
-- 'second' window was sent for a date that also has a 'first' or 'full' row:
--   SELECT user_email, send_date, count(*) FROM app_timer.daily_notifications
--   GROUP BY 1, 2 HAVING count(*) > 1;
-- Must also roll back the code to the shift-day version in the same step.
BEGIN;
ALTER TABLE app_timer.daily_notifications
    DROP CONSTRAINT IF EXISTS daily_notifications_user_email_send_date_send_window_key;
ALTER TABLE app_timer.daily_notifications
    DROP CONSTRAINT IF EXISTS daily_notifications_send_window_check;
ALTER TABLE app_timer.daily_notifications DROP COLUMN IF EXISTS send_window;
COMMIT;
