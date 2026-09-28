-- =============================================================================
-- 273_retire_gc_asset_tasks.sql
-- Retire the old GC asset-task pipeline objects (migrations 053 / 054 and the
-- gc-asset-lake migration 001). Superseded by the GC tracker sync on
-- ontel-data-platform (migrations 264 / 265 / 266 / 272): stg_gc_tracker_assets,
-- stg_gc_tracker_tasks, stg_gc_tracker_requirements and the v_gc_tracker_* views.
--
-- Why:
--   * The nightly full-refresh pipeline (053) ran in production only between
--     2026-05-20 and 2026-06-03, hit the GHA timeout and the pooler cap, and
--     its workflow has been disabled since. The gc-asset-lake redesign (001)
--     never ran: pipeline.gc_lake_runs has 0 rows.
--   * The GC tracker sync covers the same goal with more depth (requirement
--     level) and change-gated walks, and is live hourly.
--   * stg_gc_tracker_tasks holds every business column of stg_asset_tasks_gc
--     (the only columns it lacks are the surrogate id and run_id).
--
-- What is dropped (state read from the live DB on 2026-09-27):
--   analytics.mv_daily_completion_gc     5,809 rows   FROZEN SNAPSHOT, see below
--   analytics.mv_project_summary_gc      1,063 rows   FROZEN SNAPSHOT, see below
--   analytics.mv_technician_stats_gc        91 rows   FROZEN SNAPSHOT, see below
--   data_staging.stg_asset_tasks_gc          0 rows
--   data_staging.stg_assets_gc               0 rows
--   data_raw.raw_asset_tasks_gc              0 rows   (+ its id sequence)
--   pipeline.gc_lake_runs                    0 rows
--   data_raw.aggregate_assets_gc(text)       function, reads raw_asset_tasks_gc
--   analytics.refresh_one_mv(text)           REPLACED without the 3 _gc branches
--
-- FROZEN SNAPSHOT (the one thing this migration destroys):
--   The three MVs were last refreshed by the 2026-05-27 / 2026-06-03 runs and
--   never since; their fact tables were emptied afterwards. They hold task
--   aggregates for 11 Ericsson projects, approval dates 2020-05-14 to
--   2026-02-23. mv_project_summary_gc lists 1,063 non-Ontel projects but only
--   those 11 carry task counts. The rows cannot be rebuilt from our tables (the
--   sources are empty); they can be rebuilt from Swift by seeding the project
--   in the GC tracker. Tracker coverage of the 11 on 2026-09-27:
--     in the tracker (4):  Ericsson/AT&T/OH, Ericsson/AT&T/KS,
--                          Ericsson/AT&T/New England, Ericsson/T-Mobile/PA - Overlay
--     NOT in the tracker (7): Ericsson/T-Mobile/FL - Excalibur,
--                          Ericsson/T-Mobile/BAWA - Overlay, Ericsson/AT&T/STX,
--                          Ericsson/AT&T/NTX, Ericsson/T-Mobile/SFL - Excalibur,
--                          Ericsson/T-Mobile/GA - Overlay,
--                          Ericsson/T-Mobile/UPNY - Overlay
--   No consumer reads the MVs (pg_depend: no dependents; pg_stat_statements:
--   no application queries; repo grep: no code outside the retired pipeline).
--
-- NOT touched, on purpose:
--   * analytics.v_asset_gc, v_pmi_gc_aging, v_pmi_gc_aging_history: "gc" in the
--     name only; they belong to other features and have live dependents.
--   * pipeline.pipeline_runs rows named asset_tasks_gc*: run history, kept.
--   * Everything named gc_tracker.
--
-- Pre-flight run 2026-09-27 (read-only):
--   pg_depend   the only dependents of the 3 tables are the 3 MVs; the MVs,
--               gc_lake_runs and raw_asset_tasks_gc have none.
--   functions   only refresh_one_mv and aggregate_assets_gc mention them.
--   FKs / policies / publications / pg_cron jobs: none.
--   agent.schema_metadata: 0 rows for these objects (the DELETE below is a no-op
--               today, kept so the migration stays correct if rows appear).
--   No CASCADE anywhere: an unexpected dependent aborts the migration.
--
-- APPLY ORDER (do not reorder):
--   1. Merge the PR that carries this file and PUSH main: it removes
--      extract_asset_tasks_gc.py, the GC transforms, the 4 --pipeline choices
--      and pipeline-asset-tasks-gc.yml, so no code names these objects.
--   2. Apps Script: DELETE the time trigger that calls triggerAssetTasksGC
--      (Triggers page), THEN paste the whole pipeline_trigger.gs. Pasting first
--      leaves a trigger pointing at a missing function and it fails nightly.
--   3. Apply this migration.
--   4. Run the VERIFY queries at the bottom.
--
-- ROLLBACK:
--   Re-apply 053_asset_tasks_gc_tables.sql and 054_refresh_one_mv_add_gc.sql
--   (then re-add the quote and timer-revenue branches that later migrations put
--   in refresh_one_mv, and SET work_mem = '64MB' from 234), enable RLS as in
--   208 / 216, and re-apply gc-asset-lake migrations/001 for gc_lake_runs and
--   uq_stg_asset_tasks_gc_task_did. The frozen MV snapshot does NOT come back.
--
-- STATUS: DRAFT, NOT APPLIED.
-- =============================================================================

BEGIN;

-- 0. Guards -------------------------------------------------------------------
-- The drop is only safe while the fact tables are empty. A non-zero count means
-- something wrote to them after the pre-flight: stop and look.
DO $$
DECLARE
    n_raw   bigint;
    n_tasks bigint;
    n_sites bigint;
    n_runs  bigint;
BEGIN
    SELECT count(*) INTO n_raw   FROM data_raw.raw_asset_tasks_gc;
    SELECT count(*) INTO n_tasks FROM data_staging.stg_asset_tasks_gc;
    SELECT count(*) INTO n_sites FROM data_staging.stg_assets_gc;
    SELECT count(*) INTO n_runs  FROM pipeline.gc_lake_runs;
    IF n_raw + n_tasks + n_sites + n_runs > 0 THEN
        RAISE EXCEPTION
            '273 aborted: old GC tables are not empty (raw=%, tasks=%, sites=%, lake_runs=%)',
            n_raw, n_tasks, n_sites, n_runs;
    END IF;
END $$;

-- 1. refresh_one_mv without the _gc branches ----------------------------------
-- Body copied from the LIVE definition (pg_get_functiondef, 2026-09-27) minus
-- the three _gc branches. CREATE OR REPLACE keeps the owner and the grants.
CREATE OR REPLACE FUNCTION analytics.refresh_one_mv(p_view_name text)
RETURNS TABLE(view_name text, refresh_time_ms bigint)
LANGUAGE plpgsql
SECURITY DEFINER
SET statement_timeout TO '300s'
SET work_mem TO '64MB'
AS $function$
DECLARE
    start_ts TIMESTAMPTZ;
    end_ts TIMESTAMPTZ;
BEGIN
    start_ts := clock_timestamp();
    IF p_view_name = 'mv_project_summary' THEN
        REFRESH MATERIALIZED VIEW CONCURRENTLY analytics.mv_project_summary;
    ELSIF p_view_name = 'mv_technician_stats' THEN
        REFRESH MATERIALIZED VIEW CONCURRENTLY analytics.mv_technician_stats;
    ELSIF p_view_name = 'mv_daily_completion' THEN
        REFRESH MATERIALIZED VIEW CONCURRENTLY analytics.mv_daily_completion;
    ELSIF p_view_name = 'mv_quote_invoice_options' THEN
        REFRESH MATERIALIZED VIEW CONCURRENTLY analytics.mv_quote_invoice_options;
    ELSIF p_view_name = 'mv_quote_review' THEN
        REFRESH MATERIALIZED VIEW CONCURRENTLY analytics.mv_quote_review;
    ELSIF p_view_name = 'mv_quote_source_invoice_lines' THEN
        REFRESH MATERIALIZED VIEW CONCURRENTLY analytics.mv_quote_source_invoice_lines;
    ELSIF p_view_name = 'mv_timer_revenue' THEN
        REFRESH MATERIALIZED VIEW CONCURRENTLY analytics.mv_timer_revenue;
    ELSIF p_view_name = 'mv_timer_revenue_daily' THEN
        REFRESH MATERIALIZED VIEW CONCURRENTLY analytics.mv_timer_revenue_daily;
    ELSE
        RAISE EXCEPTION 'Unknown view: %', p_view_name;
    END IF;
    end_ts := clock_timestamp();
    view_name := p_view_name;
    refresh_time_ms := (EXTRACT(EPOCH FROM (end_ts - start_ts)) * 1000)::BIGINT;
    RETURN NEXT;
END;
$function$;

-- 2. Materialized views (dependents first) ------------------------------------
DROP MATERIALIZED VIEW analytics.mv_daily_completion_gc;
DROP MATERIALIZED VIEW analytics.mv_project_summary_gc;
DROP MATERIALIZED VIEW analytics.mv_technician_stats_gc;

-- 3. Function that reads the raw table ----------------------------------------
DROP FUNCTION data_raw.aggregate_assets_gc(text);

-- 4. Tables ---------------------------------------------------------------------
DROP TABLE data_staging.stg_asset_tasks_gc;
DROP TABLE data_staging.stg_assets_gc;
DROP TABLE data_raw.raw_asset_tasks_gc;
DROP TABLE pipeline.gc_lake_runs;

-- 5. Semantic-layer rows --------------------------------------------------------
DELETE FROM agent.schema_metadata
WHERE (schema_name = 'analytics'    AND table_name IN ('mv_daily_completion_gc', 'mv_project_summary_gc', 'mv_technician_stats_gc'))
   OR (schema_name = 'data_staging' AND table_name IN ('stg_asset_tasks_gc', 'stg_assets_gc'))
   OR (schema_name = 'data_raw'     AND table_name = 'raw_asset_tasks_gc')
   OR (schema_name = 'pipeline'     AND table_name = 'gc_lake_runs');

COMMIT;

-- =============================================================================
-- VERIFY (run after COMMIT; every query must return the value on the right)
--
--   SELECT count(*) FROM pg_class c JOIN pg_namespace n ON n.oid = c.relnamespace
--   WHERE (n.nspname, c.relname) IN (
--       ('analytics','mv_daily_completion_gc'), ('analytics','mv_project_summary_gc'),
--       ('analytics','mv_technician_stats_gc'), ('data_staging','stg_asset_tasks_gc'),
--       ('data_staging','stg_assets_gc'), ('data_raw','raw_asset_tasks_gc'),
--       ('data_raw','raw_asset_tasks_gc_id_seq'), ('pipeline','gc_lake_runs'));        -- 0
--
--   SELECT count(*) FROM pg_proc p JOIN pg_namespace n ON n.oid = p.pronamespace
--   WHERE n.nspname = 'data_raw' AND p.proname = 'aggregate_assets_gc';              -- 0
--
--   SELECT pg_get_functiondef('analytics.refresh_one_mv(text)'::regprocedure) ~ '_gc'; -- f
--
--   SELECT count(*) FROM pg_class c JOIN pg_namespace n ON n.oid = c.relnamespace
--   WHERE n.nspname IN ('data_raw','data_staging','analytics','reference')
--     AND c.relname LIKE '%gc_tracker%' AND c.relkind IN ('r','v');                  -- 9 (unchanged)
--
--   SELECT count(*) FROM pg_class c JOIN pg_namespace n ON n.oid = c.relnamespace
--   WHERE n.nspname = 'analytics'
--     AND c.relname IN ('v_asset_gc','v_pmi_gc_aging','v_pmi_gc_aging_history');     -- 3 (unchanged)
-- =============================================================================
