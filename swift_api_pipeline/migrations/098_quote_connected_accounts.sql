-- 098_quote_connected_accounts.sql
-- Applied live 2026-06-12 via Supabase MCP apply_migration.
--
-- Connected Accounts Phase A (quote-automation spec 2026-06-12): per-user Gmail sender
-- connections + a per-user settings table holding the active sender.
-- The queue.sender_email FK to connections(email) is dropped because per-user keying
-- makes email non-unique; the queue keeps sender_email as plain text (display + token
-- lookup, not integrity).

ALTER TABLE data_staging.stg_quote_email_queue
  DROP CONSTRAINT IF EXISTS stg_quote_email_queue_sender_email_fkey;

ALTER TABLE data_staging.stg_quote_gmail_connections
  DROP CONSTRAINT IF EXISTS stg_quote_gmail_connections_pkey;

ALTER TABLE data_staging.stg_quote_gmail_connections
  ADD COLUMN IF NOT EXISTS id bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY;

ALTER TABLE data_staging.stg_quote_gmail_connections
  ADD CONSTRAINT stg_quote_gmail_connections_owner_email_uniq UNIQUE (connected_by, email);

CREATE TABLE IF NOT EXISTS data_staging.stg_quote_user_settings (
  user_email          text PRIMARY KEY,
  active_sender_email text,
  updated_at          timestamptz NOT NULL DEFAULT now()
);

-- Back-compat: each existing connection becomes its owner's active sender.
INSERT INTO data_staging.stg_quote_user_settings (user_email, active_sender_email)
SELECT DISTINCT ON (connected_by) connected_by, email
FROM data_staging.stg_quote_gmail_connections
WHERE status = 'active'
ORDER BY connected_by, connected_at DESC
ON CONFLICT (user_email) DO NOTHING;

GRANT SELECT, INSERT, UPDATE, DELETE ON data_staging.stg_quote_user_settings TO service_role;
