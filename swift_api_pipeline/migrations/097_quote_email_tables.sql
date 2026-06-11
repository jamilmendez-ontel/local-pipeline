-- 097_quote_email_tables.sql
-- Applied live 2026-06-11 via Supabase MCP apply_migration.
--
-- Quote email send feature (quote-automation spec 2026-06-11): per-user Gmail
-- connections, reusable email templates, and the scheduled-send queue.
-- App-owned side tables, service-role access only (same trust model as
-- stg_quote_overrides / stg_quote_generated). refresh_token_enc is AES-256-GCM
-- encrypted by the app (key in app env GMAIL_TOKEN_KEY); the DB never sees the
-- plaintext refresh token.

CREATE TABLE data_staging.stg_quote_gmail_connections (
  email            text PRIMARY KEY,
  refresh_token_enc text NOT NULL,          -- AES-256-GCM, key in app env GMAIL_TOKEN_KEY
  connected_by     text NOT NULL,
  connected_at     timestamptz NOT NULL DEFAULT now(),
  status           text NOT NULL DEFAULT 'active',  -- 'active' | 'error'
  last_error       text
);

CREATE TABLE data_staging.stg_quote_email_templates (
  id         bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
  name       text NOT NULL UNIQUE,
  subject    text NOT NULL,
  body_html  text NOT NULL,
  updated_by text NOT NULL,
  updated_at timestamptz NOT NULL DEFAULT now()
);

CREATE TABLE data_staging.stg_quote_email_queue (
  id               bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
  task_did         text NOT NULL,
  sender_email     text NOT NULL REFERENCES data_staging.stg_quote_gmail_connections(email),
  gmail_draft_id   text NOT NULL,
  scheduled_at     timestamptz NOT NULL,
  status           text NOT NULL DEFAULT 'scheduled',  -- scheduled|sent|failed|cancelled
  subject_resolved text NOT NULL,
  to_resolved      text NOT NULL,
  cc_resolved      text,
  template_name    text,
  created_by       text NOT NULL,
  created_at       timestamptz NOT NULL DEFAULT now(),
  sent_at          timestamptz,
  gmail_message_id text,
  error            text
);

CREATE INDEX stg_quote_email_queue_due_idx
  ON data_staging.stg_quote_email_queue (status, scheduled_at);

GRANT SELECT, INSERT, UPDATE, DELETE ON data_staging.stg_quote_gmail_connections TO service_role;
GRANT SELECT, INSERT, UPDATE, DELETE ON data_staging.stg_quote_email_templates TO service_role;
GRANT SELECT, INSERT, UPDATE, DELETE ON data_staging.stg_quote_email_queue TO service_role;
