-- 233: stop the timer-rollup refresh from spilling ~169 MB temp per run (Jamil 2026-08-13).
--
-- Why: analytics.refresh_timer_day_rollup_safe() (pg_cron */10 + health-watcher auto-reset)
-- rebuilds mv_timer_day_rollup over ~390k stg_timer_activities_clean rows. At the default
-- work_mem (3500kB) every sort/hash in the plan spills: measured ~117 MB temp on the SELECT
-- side alone, ~169 MB per call including the CONCURRENTLY diff, ~24 GB/day of disk writes.
-- Measured with SET LOCAL work_mem='64MB': zero temp blocks; largest node (HashAggregate)
-- needs 57.4 MB, so 64 MB is the minimum that fully eliminates spill. Peak transient memory
-- ~200 MB for ~9s, single-flight (second caller blocks on the CONCURRENTLY lock), safe on
-- this compute tier. Function-scoped: no other session or query inherits it.
--
-- Deliberately NOT raising work_mem globally, and NOT reworking the MV into an
-- incrementally-maintained table (would cascade through mv_hr_report_review, the
-- never-drop brownout MV, plus v_daily_report_approvals / v_unmatched_timer_emails).

ALTER FUNCTION analytics.refresh_timer_day_rollup_safe() SET work_mem = '64MB';

-- Verify:
--   SELECT proconfig FROM pg_proc p JOIN pg_namespace n ON n.oid = p.pronamespace
--   WHERE n.nspname = 'analytics' AND p.proname = 'refresh_timer_day_rollup_safe';
--   -- expect: {statement_timeout=180s,"work_mem=64MB"}
-- Then confirm temp_blks_written for the refresh stops growing in pg_stat_statements.
--
-- Rollback:
--   ALTER FUNCTION analytics.refresh_timer_day_rollup_safe() RESET work_mem;
