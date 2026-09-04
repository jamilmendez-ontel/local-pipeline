-- 254: cut the IO of the two HR rollup refresh jobs (Jamil 2026-09-04, "fix both").
-- APPLIED to prod 2026-09-04 ~04:40 ET (both statements, live, before this file was
-- committed); numbers below are measured on prod, not estimated.
--
-- Context: after the 02:57-04:06 ET outage the team suspected analytics.mv_timer_day_rollup,
-- mv_daily_report_task_rollup and mv_hr_report_review of "taking too much usage". Measured
-- over 24 h of cron.job_run_details they cost ~42 min/day of one connection (~3% duty):
-- job 9 (task rollup + hr_review, */5) 260 runs avg 5.4 s; job 11 (timer, */10) 130 runs
-- avg 8.7 s. Not the top load (rebuild_timer_clean, asset-task reloads, gap_report and
-- scorecard cycles all cost more). Two genuine inefficiencies remained, fixed here.
--
-- 1) refresh_dr_task_rollup_safe() / refresh_hr_report_review_safe() still ran at the
--    default work_mem (3500kB). Migration 233 raised work_mem ONLY on the timer function.
--    Measured: 8 calls since the 04:06 restart wrote 19,448 temp blocks = ~19 MB temp per
--    call x 288 calls/day = ~5.5 GB/day of temp writes. Same fix as 233, function-scoped:
--    nothing else inherits it. The nested PERFORM inherits the outer setting; the hr_review
--    function gets its own for the health-watcher / manual call path.
ALTER FUNCTION analytics.refresh_dr_task_rollup_safe()   SET work_mem = '64MB';
ALTER FUNCTION analytics.refresh_hr_report_review_safe() SET work_mem = '64MB';

-- 2) The timer rollup refresh seq-scanned the whole stg_timer_activities_clean heap
--    (125 MB, 15,940 pages) on every run, and with shared_buffers = 256 MB those pages
--    do not survive between the 144 daily refreshes: EXPLAIN (ANALYZE, BUFFERS) of the
--    MV's SELECT read 15,940 pages from disk (0 hits) 13 min after a restart. The MV
--    only needs (user_email, start_time, end_time). A covering btree on exactly those
--    columns is 21 MB / 2,739 pages and the planner switches to an Index Only Scan:
--      before: Seq Scan, shared read=15,940      Execution 5,357 ms
--      after : Index Only Scan, Heap Fetches 0, shared read=2,718 cold / read=0 hit=2,965 warm
--              Execution 5,268 / 5,077 ms (the rest of the plan is in-memory sort/window
--              work; the win is IO, not CPU).
--    Caveat: rebuild_timer_clean() (hourly, DELETE + INSERT of all ~400k rows) clears the
--    visibility map, so the first refresh after each rebuild pays heap fetches until
--    autovacuum sets the bits again; the other five refreshes per hour do not. The index
--    is also one more index rebuild_timer_clean maintains (~21 MB/hour of index writes vs
--    ~125 MB x 6 refreshes/hour of heap reads saved).
--    CONCURRENTLY so it never blocks the 10-min refresh or the hourly rebuild; cannot run
--    inside a transaction block, so apply this statement in autocommit mode (it took 203 s
--    on prod under the rebuild churn).
CREATE INDEX CONCURRENTLY IF NOT EXISTS idx_stg_timer_clean_email_start_end
  ON data_staging.stg_timer_activities_clean USING btree (user_email, start_time, end_time);

-- Verify:
--   SELECT proname, proconfig FROM pg_proc p JOIN pg_namespace n ON n.oid = p.pronamespace
--   WHERE n.nspname = 'analytics' AND proname LIKE 'refresh_%_safe';
--   -- expect work_mem=64MB on all three
--   SELECT indisvalid FROM pg_index WHERE indexrelid = 'data_staging.idx_stg_timer_clean_email_start_end'::regclass;
--   SELECT calls, temp_blks_written, shared_blks_read FROM pg_stat_statements
--   WHERE query LIKE 'SELECT analytics.refresh_dr_task_rollup_safe()%';   -- temp stops growing
--   EXPLAIN (ANALYZE, BUFFERS) <pg_get_viewdef('analytics.mv_timer_day_rollup')>  -- Index Only Scan
--
-- Deliberately NOT done: windowing or redefining mv_timer_day_rollup. An MV cannot be
-- redefined in place; DROP cascades through mv_hr_report_review -> v_hr_report_review ->
-- hr_review_page (the NEVER-DROP brownout chain, migration 207).
--
-- Rollback:
--   ALTER FUNCTION analytics.refresh_dr_task_rollup_safe()   RESET work_mem;
--   ALTER FUNCTION analytics.refresh_hr_report_review_safe() RESET work_mem;
--   DROP INDEX CONCURRENTLY IF EXISTS data_staging.idx_stg_timer_clean_email_start_end;
