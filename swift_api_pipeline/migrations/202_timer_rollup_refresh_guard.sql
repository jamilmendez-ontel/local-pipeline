-- 202: stop mv_timer_day_rollup from ever being blanked (Jamil 2026-07-30).
--
-- Problem: the pg_cron job `refresh_mv_timer_day_rollup` ran a bare
--   REFRESH MATERIALIZED VIEW CONCURRENTLY analytics.mv_timer_day_rollup
-- every 10 min. A CONCURRENTLY refresh rebuilds from the source and applies the
-- diff as DELETE+INSERT (verified: the MV's n_tup_upd is 0, n_tup_ins ~= n_tup_del).
-- So if it reads a transiently-empty data_staging.stg_timer_activities_clean it
-- DELETEs every row and inserts none -> the MV goes to 0 rows -> coverage_pct
-- goes NULL across v_hr_report_review -> the Hours Variance dashboard + DR
-- Monitoring timer/variance columns blank out. With ~30% of the cron runs failing
-- ("job startup timeout"), it could then stay blank for a long time. Observed
-- twice on 2026-07-30.
--
-- Fix: refresh through a guard function that SKIPS the refresh when the clean
-- source is empty, so a transiently-empty source leaves the MV STALE (its last
-- good rows), never blank. Repoint the existing cron job at the guard. This is
-- the bulletproof layer: the MV can no longer be emptied by a refresh regardless
-- of what blips the source. (Root-cause work on the source-empty window --
-- atomic reload + workflow concurrency -- is handled separately.)

CREATE OR REPLACE FUNCTION analytics.refresh_timer_day_rollup_safe()
 RETURNS void
 LANGUAGE plpgsql
 SECURITY DEFINER
 SET statement_timeout TO '180s'
AS $function$
BEGIN
  -- Never rebuild the rollup from an empty source: that is what blanks the HR
  -- dashboards. Skip this tick; the MV keeps its last good rows until the source
  -- is repopulated and a later tick refreshes normally. EXISTS short-circuits.
  IF NOT EXISTS (SELECT 1 FROM data_staging.stg_timer_activities_clean LIMIT 1) THEN
    RAISE NOTICE 'refresh_timer_day_rollup_safe: stg_timer_activities_clean is empty, skipping refresh';
    RETURN;
  END IF;
  REFRESH MATERIALIZED VIEW CONCURRENTLY analytics.mv_timer_day_rollup;
END;
$function$;

-- Repoint the existing every-10-min cron job at the guard (keeps the same
-- schedule/jobname; only the command changes). Looked up by jobname so this is
-- environment-independent.
SELECT cron.alter_job(
  (SELECT jobid FROM cron.job WHERE jobname = 'refresh_mv_timer_day_rollup'),
  command => 'SELECT analytics.refresh_timer_day_rollup_safe();'
);

-- Rollback: repoint the cron back at the bare refresh and drop the function:
--   SELECT cron.alter_job((SELECT jobid FROM cron.job WHERE jobname='refresh_mv_timer_day_rollup'),
--     command => 'REFRESH MATERIALIZED VIEW CONCURRENTLY analytics.mv_timer_day_rollup');
--   DROP FUNCTION analytics.refresh_timer_day_rollup_safe();
