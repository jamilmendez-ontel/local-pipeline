-- 102_quote_email_queue_returned_columns.sql
--
-- "Return to tab" feature: a queued email can be returned to the Queue or the
-- Generated "to schedule" list. The row is KEPT as an annotated terminal audit
-- record; returned_at IS NOT NULL means "released" (no longer holds the quote in
-- the Outbox). Additive, nullable columns; existing rows unaffected.

ALTER TABLE data_staging.stg_quote_email_queue
  ADD COLUMN IF NOT EXISTS returned_to text,   -- 'queue' | 'generated'
  ADD COLUMN IF NOT EXISTS returned_by text,   -- email of the user who returned it
  ADD COLUMN IF NOT EXISTS returned_at timestamptz;
