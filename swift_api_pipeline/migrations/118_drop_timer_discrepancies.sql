-- Migration 118: Drop the retired Timer Discrepancies pipeline objects
--
-- The old Google-Form timer-discrepancy reporting pipeline was superseded by
-- the timer correction system (timer_correction_review.py + migration 117
-- app_timer cutover) and fully retired 2026-06-22 (workflow + triggers removed,
-- commit 9bc1fc8). These objects no longer receive new data.
--
-- Pre-flight verified 2026-06-22:
--   * No external view/MV/function/foreign key depends on them (only the view
--     depends on its own staging table, dropped here).
--   * No code reference outside the old pipeline's own files.
--   * The timer CORRECTION system does NOT touch these objects (it uses
--     stg_timer_activities / stg_timer_activities_clean + the app_timer schema).
--   * agent.schema_metadata holds 33 descriptive rows for these objects that
--     DARA's semantic layer reads dynamically -- removed here so DARA cannot
--     generate queries against dropped objects.
--
-- Indexes, identity sequences, TOAST tables, and RLS policies drop automatically
-- with their parent tables. Structure (if ever needed again) is reconstructable
-- from migrations 034/035/046. NOTE: this permanently deletes 5,234 historical
-- form-submission rows; there is no data rollback.

BEGIN;

-- 1. Analytics serving view (depends on the staging table)
DROP VIEW IF EXISTS analytics.v_timer_discrepancies;

-- 2. Staging table (+ its indexes/sequence cascade automatically)
DROP TABLE IF EXISTS data_staging.stg_timer_discrepancies;

-- 3. Raw landing table (+ its index/sequence cascade automatically)
DROP TABLE IF EXISTS data_raw.raw_timer_discrepancies;

-- 4. DARA semantic-layer metadata for the three objects (33 rows)
DELETE FROM agent.schema_metadata
 WHERE (schema_name, table_name) IN
   (('analytics',     'v_timer_discrepancies'),
    ('data_staging',  'stg_timer_discrepancies'),
    ('data_raw',      'raw_timer_discrepancies'));

COMMIT;
