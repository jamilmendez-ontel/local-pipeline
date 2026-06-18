-- 107_quote_swift_account.sql
-- Per-user Swift API credentials for the quote app (Phase B credential capture).
-- Additive, nullable. Password stored as AES-256-GCM ciphertext (never plaintext).
-- NULL swift_username = not connected.
ALTER TABLE data_staging.stg_quote_user_settings
  ADD COLUMN IF NOT EXISTS swift_username     TEXT,
  ADD COLUMN IF NOT EXISTS swift_password_enc TEXT,
  ADD COLUMN IF NOT EXISTS swift_verified_at  TIMESTAMPTZ;
