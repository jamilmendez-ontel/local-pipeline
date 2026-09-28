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
--   The snapshot is NOT trustworthy data. Every one of the 11 projects has a
--   total_tasks that is an exact multiple of 1,000 (351,000 to 363,000), and
--   within a project every daily row carries the same tasks_completed, equal
--   to total_tasks / 1000: each project is 1,000 tasks repeated 351-363 times
--   by the old loader. Counts read from these MVs were never real.
--   Exported anyway before the drop (2026-09-28, rows equal to live):
--   local-pipeline/out/gc-mv-snapshot-2026-09-28/*.csv (gitignored).
--   No consumer reads the MVs (pg_depend: no dependents; pg_stat_statements:
--   no application queries; repo grep: no code outside the retired pipeline).
--   Grants that go away with them: anon and authenticated had SELECT on the
--   three MVs (inert, neither role has USAGE on analytics); tdt_reader had
--   SELECT on the two empty staging tables.
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
--               One row ABOUT another table names a dropped MV: the row for
--               data_staging.stg_timer_activities, and that table's COMMENT.
--               Both are corrected in section 5.
--   sequences   the _gc id defaults use stg_asset_tasks_id_seq / stg_assets_id_seq,
--               owned by the Ontel tables, so they survive. Only
--               raw_asset_tasks_gc_id_seq goes, with its table.
--   evidence window: the server restarted 2026-09-16 09:19 ET, so pg_stat_*
--               covers 12 days, not all time. Independent proof that nothing
--               refreshes the _gc MVs: they still hold rows while their sources
--               are empty, and a refresh would have emptied them.
--   No CASCADE anywhere: an unexpected dependent aborts the migration.
--   Independent second pre-flight (premerge-review lane C, 2026-09-28): SAFE TO
--   APPLY; its findings are folded into this file.
--
-- APPLY ORDER:
--   1. Merge the PR that carries this file and PUSH main: it removes
--      extract_asset_tasks_gc.py, the GC transforms, the 4 --pipeline choices
--      and pipeline-asset-tasks-gc.yml, so no code names these objects.
--   2. Apply this migration OUTSIDE the nightly windows: asset_tasks_extract
--      runs about 00:19-00:57 ET, targeted_asset_tasks about 02:36-02:44 ET.
--      Replacing refresh_one_mv does not disturb a call already in flight.
--   3. Run the VERIFY queries at the bottom.
--   Independent of 2 and 3, any time after 1 (Jamil, by hand): Apps Script,
--      DELETE the time trigger that calls triggerAssetTasksGC (Triggers page),
--      THEN paste the whole pipeline_trigger.gs. Pasting first leaves a trigger
--      pointing at a missing function and it fails nightly. Until then the
--      trigger fires a dispatch no workflow listens to, which is harmless.
--   Also outside this migration: the gc-asset-lake folder (hottier.py, state.py
--      and two tests name stg_asset_tasks_gc / gc_lake_runs). It was never
--      scheduled or deployed; archive it.
--
-- ROLLBACK:
--   a. refresh_one_mv: DO NOT re-apply 054_refresh_one_mv_add_gc.sql. It is a
--      CREATE OR REPLACE from May with 6 branches and no work_mem: it would
--      remove the 3 quote and 2 timer-revenue branches and every such refresh
--      (about 30 calls a day) would raise 'Unknown view'. Instead re-run
--      section 1 of THIS file with these three branches added back after the
--      mv_daily_completion branch:
--          ELSIF p_view_name = 'mv_project_summary_gc' THEN
--              REFRESH MATERIALIZED VIEW CONCURRENTLY analytics.mv_project_summary_gc;
--          ELSIF p_view_name = 'mv_technician_stats_gc' THEN
--              REFRESH MATERIALIZED VIEW CONCURRENTLY analytics.mv_technician_stats_gc;
--          ELSIF p_view_name = 'mv_daily_completion_gc' THEN
--              REFRESH MATERIALIZED VIEW CONCURRENTLY analytics.mv_daily_completion_gc;
--      (the MVs must exist first, so do b before a).
--   b. Tables, function, MVs: re-apply 053_asset_tasks_gc_tables.sql, enable RLS
--      as in 208 / 216, then gc-asset-lake migrations/001 for gc_lake_runs and
--      uq_stg_asset_tasks_gc_task_did. 053 clones the Ontel tables with
--      LIKE ... INCLUDING ALL, and those have drifted since May, so the result
--      is NOT identical to what was dropped: stg_assets_gc gains
--      asset_identifier (18 columns, was 17) and the three tables come back
--      with more indexes (9 / 14 / 3, were 8 / 9 / 1). service_role and
--      tdt_reader grants return through the default ACLs; the anon and
--      authenticated SELECT on the MVs does not, which is the safer state.
--   c. Code: revert the PR that carries this file (extractor, transforms,
--      --pipeline choices, workflow, triggerAssetTasksGC).
--   d. Section 5: restore the two texts by putting ", mv_project_summary_gc"
--      back after "mv_project_summary".
--   The MVs come back EMPTY. Their old rows exist only in the CSV export.
--
-- STATUS: APPLIED 2026-09-28 01:10:57 ET via MCP apply_migration (recorded as
--   20260928051057 273_retire_gc_asset_tasks), after local-pipeline PR #84 merged
--   as 912b2da (04:35:52 UTC) and the nightly asset_tasks_extract had finished
--   (00:19-00:57 ET). 19 post-merge workflow runs on 912b2da were green before
--   the apply.
-- BEFORE: MVs 5,809 / 1,063 / 91 rows; the 4 tables 0 rows; agent.schema_metadata
--   972 rows; no other session held a lock on any target; no refresh in flight.
-- VERIFIED (01:11:16 ET), every VERIFY query below returned its expected value:
--   dropped objects left 0; aggregate_assets_gc 0; refresh_one_mv mentions _gc = f
--   with 8 REFRESH branches, SECURITY DEFINER, config {statement_timeout=300s,
--   work_mem=64MB}, ACL unchanged (PUBLIC, postgres, service_role); gc_tracker
--   objects 9; v_asset_gc / v_pmi_gc_aging / v_pmi_gc_aging_history 3;
--   stale semantic-layer texts 0; agent.schema_metadata still 972 rows (0 deleted,
--   1 updated); analytics.v_page5_po_status still selects.
-- STILL OPEN (by hand, Jamil): Apps Script, delete the triggerAssetTasksGC time
--   trigger, then paste the whole pipeline_trigger.gs.
-- =============================================================================

BEGIN;

-- Fail fast instead of queueing ACCESS EXCLUSIVE locks behind a long reader.
SET LOCAL lock_timeout = '5s';

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

-- The timer fact's description and table comment list mv_project_summary_gc as
-- a reader. Both texts are otherwise unchanged.
UPDATE agent.schema_metadata
SET business_context = replace(business_context, 'mv_project_summary, mv_project_summary_gc', 'mv_project_summary'),
    updated_at = now()
WHERE schema_name = 'data_staging'
  AND table_name = 'stg_timer_activities'
  AND business_context LIKE '%mv_project_summary, mv_project_summary_gc%';

COMMENT ON TABLE data_staging.stg_timer_activities IS
    'CANONICAL raw timer fact (append/write-once per run). Serving layer reads this: analytics.v_timer_activities, mv_project_summary. Derivative: data_staging.stg_timer_activities_clean (do not confuse).';

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
--
--   SELECT (SELECT count(*) FROM agent.schema_metadata
--            WHERE coalesce(description,'') || coalesce(business_context,'') || coalesce(data_notes,'')
--                  ~ '(mv_[a-z_]+_gc|stg_asset_tasks_gc|stg_assets_gc|raw_asset_tasks_gc|gc_lake_runs)')
--        + (SELECT count(*) FROM pg_description
--            WHERE description ~ '(mv_[a-z_]+_gc|stg_asset_tasks_gc|stg_assets_gc|raw_asset_tasks_gc|gc_lake_runs)'); -- 0
-- =============================================================================
