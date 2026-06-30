-- 132: Give raw_daily_reports a real natural-key unique constraint so the
-- extractor can upsert (replace-on-change, append-when-new) instead of appending.
--
-- Problem: every pipeline run (~145/day) re-inserted its full re-fetched window as
-- brand-new rows. The only unique constraint was the bigserial PK, so the inserts'
-- `ON CONFLICT DO NOTHING` never had an arbiter to dedup against. The table grew to
-- ~13.3M rows / 29 GB of duplicate JSONB while the served staging tables stayed flat
-- (staging already upserts on its own keys). Nothing downstream reads raw_daily_reports
-- (no view/matview/function depends on it; the extractor writes staging directly from
-- the same payload), so it was pure audit dead-weight.
--
-- Fix: add UNIQUE (source_type, source_id). Verified against live data that this pair
-- is non-null and unique within a run for all three source types (task/requirement/timer).
--
-- NOTE: on the LIVE database the existing duplicates must be cleared with a one-time
-- TRUNCATE before this runs, because the constraint cannot be added while dup keys
-- exist. On a fresh rebuild the table is already empty, so this file is safe as-is.

-- The non-unique (source_type, source_id) index is superseded by the unique one below.
DROP INDEX IF EXISTS data_raw.idx_raw_dr_source;

DO $$
BEGIN
  IF NOT EXISTS (
    SELECT 1 FROM pg_constraint
    WHERE conname = 'raw_daily_reports_source_uk'
      AND conrelid = 'data_raw.raw_daily_reports'::regclass
  ) THEN
    ALTER TABLE data_raw.raw_daily_reports
      ADD CONSTRAINT raw_daily_reports_source_uk UNIQUE (source_type, source_id);
  END IF;
END $$;
