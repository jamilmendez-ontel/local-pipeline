-- Migration 050: Track entry-id set per daily email to support re-send
-- when entries materialize or are added after the original email goes out.
--
-- Background: stg_timer_daily_notifications stores thread_id/message_id
-- so confirmation emails can reply in-thread. To detect when a day needs
-- a fresh email (NULL-end timers stopped, manual addition appeared, etc.)
-- we record the set of entry_ids that were in the most-recent email. A
-- new entry_id appearing for that date means the day's view has changed
-- and a re-send is warranted.

ALTER TABLE data_staging.stg_timer_daily_notifications
    ADD COLUMN IF NOT EXISTS last_sent_at TIMESTAMPTZ,
    ADD COLUMN IF NOT EXISTS last_sent_entry_ids JSONB;

-- Backfill: existing rows reflect the original-send state.
-- created_at is the moment the original daily email went out, and we
-- don't know which entry_ids it contained, so leave last_sent_entry_ids
-- NULL for now. The next re-send pass will treat NULL as "no snapshot"
-- and either populate it from current state (silent first-time bootstrap)
-- or emit a re-send if today's set obviously differs from yesterday's
-- snapshot — see find_days_needing_resend() for the gating rule.
UPDATE data_staging.stg_timer_daily_notifications
SET last_sent_at = created_at
WHERE last_sent_at IS NULL;

CREATE INDEX IF NOT EXISTS idx_stg_timer_daily_notifs_last_sent
    ON data_staging.stg_timer_daily_notifications(last_sent_at);
