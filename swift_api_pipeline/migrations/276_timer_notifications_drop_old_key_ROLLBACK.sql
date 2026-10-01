-- 276_timer_notifications_drop_old_key_ROLLBACK.sql
-- Re-adds the old 2-column key. Precondition: no (user_email, send_date) has
-- more than one row (see the 275 rollback header for the check query).
BEGIN;
ALTER TABLE app_timer.daily_notifications
    ADD CONSTRAINT daily_notifications_user_email_send_date_key UNIQUE (user_email, send_date);
COMMIT;
