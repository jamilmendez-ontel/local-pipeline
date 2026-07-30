-- 203: same blank-guard as migration 202, for the OTHER HR rollup MV
-- (Jamil 2026-07-30). analytics.mv_daily_report_task_rollup feeds
-- v_hr_report_review (first_clock_in / total_hours) and the DR Monitoring page.
-- It is refreshed every 5 min by the pg_cron job `refresh_mv_daily_report_task_rollup`
-- with a bare REFRESH ... CONCURRENTLY, so -- exactly like the timer rollup fixed
-- in 202 -- a transiently-empty source would let the refresh delete every row and
-- insert none, blanking DR Monitoring. ~28% of its runs also fail with
-- "job startup timeout", which would keep it stuck.
--
-- The MV is a FULL JOIN of data_staging.stg_daily_report_hours and
-- data_staging.stg_daily_report_attendance. Skip the refresh when EITHER source
-- is empty (e.g. mid non-atomic reload), so the MV stays fully consistent with
-- its last good rows -- stale, never blank/half-empty -- until both sources are
-- populated and a later tick refreshes normally.

CREATE OR REPLACE FUNCTION analytics.refresh_dr_task_rollup_safe()
 RETURNS void
 LANGUAGE plpgsql
 SECURITY DEFINER
 SET statement_timeout TO '180s'
AS $function$
BEGIN
  IF NOT EXISTS (SELECT 1 FROM data_staging.stg_daily_report_hours LIMIT 1)
     OR NOT EXISTS (SELECT 1 FROM data_staging.stg_daily_report_attendance LIMIT 1) THEN
    RAISE NOTICE 'refresh_dr_task_rollup_safe: a daily-report source is empty, skipping refresh';
    RETURN;
  END IF;
  REFRESH MATERIALIZED VIEW CONCURRENTLY analytics.mv_daily_report_task_rollup;
END;
$function$;

SELECT cron.alter_job(
  (SELECT jobid FROM cron.job WHERE jobname = 'refresh_mv_daily_report_task_rollup'),
  command => 'SELECT analytics.refresh_dr_task_rollup_safe();'
);

-- Rollback: repoint the cron back at the bare refresh and drop the function:
--   SELECT cron.alter_job((SELECT jobid FROM cron.job WHERE jobname='refresh_mv_daily_report_task_rollup'),
--     command => 'REFRESH MATERIALIZED VIEW CONCURRENTLY analytics.mv_daily_report_task_rollup');
--   DROP FUNCTION analytics.refresh_dr_task_rollup_safe();
