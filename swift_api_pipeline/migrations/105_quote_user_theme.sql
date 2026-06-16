-- 105_quote_user_theme.sql
-- Per-user UI theme preference for the quote app. Additive, nullable.
-- NULL or 'ledger' = the default Ledger theme; 'ontel' = the Ontel theme.
ALTER TABLE data_staging.stg_quote_user_settings
  ADD COLUMN IF NOT EXISTS theme TEXT;
