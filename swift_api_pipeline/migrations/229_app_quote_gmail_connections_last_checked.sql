-- 229: quote-automation gmail connection health probe bookkeeping.
-- The dispatcher probes at most one active connection per run whose
-- last_checked_at is NULL or older than 24h, and stamps it here.
-- Rollback: alter table app_quote.gmail_connections drop column last_checked_at;
alter table app_quote.gmail_connections
  add column if not exists last_checked_at timestamptz;
