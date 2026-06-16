-- 103_quote_send_as_identity.sql
-- "Send as" identity feature. Additive, nullable, reversible.
-- active_from_email: user's saved default From (NULL = send as own account address).
-- from_email: the actual From used on a queued send (NULL = own account = sender_email).
ALTER TABLE data_staging.stg_quote_user_settings
  ADD COLUMN IF NOT EXISTS active_from_email TEXT;

ALTER TABLE data_staging.stg_quote_email_queue
  ADD COLUMN IF NOT EXISTS from_email TEXT;
