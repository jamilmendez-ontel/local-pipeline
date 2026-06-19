-- ═══════════════════════════════════════════════════════════════════════════════
-- 114 — OntelDB reorg Phase B: resolve timer-table ambiguity (document, do NOT drop)
-- ═══════════════════════════════════════════════════════════════════════════════
-- Companion: ai-projects/DATABASE_REORG_PLAN.md (Phase B).
--
-- DECISION (evidence-based, 2026-06-19): the two timer tables are NOT duplicates — they
-- are two distinct concepts, both live. The plan's "drop the other" premise did not hold,
-- so per the plan's own escape hatch ("if both are needed, comment each with its purpose
-- so the ambiguity is gone") we KEEP BOTH and document them. Nothing is dropped.
--
--   data_staging.stg_timer_activities        = CANONICAL raw timer fact (append/write-once).
--       The analytics serving layer reads THIS table: analytics.v_timer_activities,
--       mv_project_summary, mv_project_summary_gc. backfill_asset_did() also targets it.
--   data_staging.stg_timer_activities_clean  = dedup + correction-applied DERIVATIVE,
--       rebuilt by data_staging.rebuild_timer_clean() from the canonical table (removes
--       rejected duplicate reviews, applies corrections, injects manual additions, excludes
--       removals). Consumed by the timer-correction reporting workflow + DARA.
--       It is NOT canonical and is never written by the extract pipeline directly.
--
-- Verified consumers before deciding: v_timer_activities + mv_project_summary(+_gc) read
-- the base table; rebuild_timer_clean() (called by timer_correction_review.py and the
-- import scripts) is the sole writer of _clean; DARA + export scripts read _clean.
--
-- Also: drop one redundant duplicate index on _clean (two identical (project_did) indexes).
-- Tagged backups for Phase A live in schema reorg_backup_20260619 (Phase B moves no data).
-- ═══════════════════════════════════════════════════════════════════════════════

BEGIN;

-- Canonical raw fact
COMMENT ON TABLE data_staging.stg_timer_activities IS
  'CANONICAL raw timer fact (append/write-once per run). Serving layer reads this: '
  'analytics.v_timer_activities, mv_project_summary, mv_project_summary_gc. '
  'Derivative: data_staging.stg_timer_activities_clean (do not confuse).';

UPDATE agent.schema_metadata
   SET description     = 'GPS-tracked time logs for technician site visits — CANONICAL raw timer fact',
       business_context= 'Canonical raw timer fact, append/write-once per pipeline run. The analytics serving '
                         'layer (v_timer_activities, mv_project_summary, mv_project_summary_gc) and '
                         'backfill_asset_did() read THIS table. Source of truth for raw timer activity.',
       data_notes      = 'The dedup + correction-applied version is data_staging.stg_timer_activities_clean '
                         '(a derivative rebuilt by rebuild_timer_clean()). Use this table for raw activity.',
       related_tables  = ARRAY['data_staging.stg_timer_activities_clean'],
       updated_at      = now()
 WHERE schema_name='data_staging' AND table_name='stg_timer_activities' AND column_name IS NULL;

-- Dedup + correction-applied derivative
COMMENT ON TABLE data_staging.stg_timer_activities_clean IS
  'DERIVATIVE of stg_timer_activities, rebuilt by data_staging.rebuild_timer_clean(): removes '
  'rejected duplicate reviews, applies timer corrections, injects manual additions, excludes '
  'removals. Consumed by the timer-correction reporting workflow + DARA. NOT the canonical raw '
  'fact (that is data_staging.stg_timer_activities). Never written by the extract pipeline directly.';

UPDATE agent.schema_metadata
   SET description     = 'Dedup + correction-applied derivative of stg_timer_activities',
       business_context= 'Rebuilt by data_staging.rebuild_timer_clean() from stg_timer_activities: removes '
                         'rejected duplicate reviews, applies corrections, injects manual additions, excludes '
                         'removals. Consumed by the timer-correction reporting workflow and DARA. NOT canonical.',
       data_notes      = 'Derived table; never written by the extract pipeline directly. The canonical raw '
                         'fact is data_staging.stg_timer_activities.',
       related_tables  = ARRAY['data_staging.stg_timer_activities','data_staging.stg_timer_corrections',
                               'data_staging.stg_timer_entry_removals','data_staging.stg_timer_entry_additions',
                               'data_staging.stg_timer_duplicate_reviews'],
       updated_at      = now()
 WHERE schema_name='data_staging' AND table_name='stg_timer_activities_clean' AND column_name IS NULL;

-- Cleanup: drop the redundant duplicate (project_did) index on the derivative.
DROP INDEX IF EXISTS data_staging.stg_timer_activities_clean_project_did_idx1;

COMMIT;
