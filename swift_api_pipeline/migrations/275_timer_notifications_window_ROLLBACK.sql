-- 275_timer_notifications_window_ROLLBACK.sql
-- Reverts migration 275. Valid only while no (user_email, send_date) has more
-- than one row, i.e. before any 'second' window has been sent for a date that
-- also has a 'first' or 'full' row. Check first:
--   SELECT user_email, send_date, count(*) FROM app_timer.daily_notifications
--   GROUP BY 1, 2 HAVING count(*) > 1;
-- Must also roll back the code to the shift-day version in the same step.
BEGIN;
ALTER TABLE app_timer.daily_notifications
    DROP CONSTRAINT IF EXISTS daily_notifications_user_email_send_date_window_key;
ALTER TABLE app_timer.daily_notifications
    DROP CONSTRAINT IF EXISTS daily_notifications_window_check;
ALTER TABLE app_timer.daily_notifications
    ADD CONSTRAINT daily_notifications_user_email_send_date_key UNIQUE (user_email, send_date);
ALTER TABLE app_timer.daily_notifications DROP COLUMN IF EXISTS window;
COMMIT;
