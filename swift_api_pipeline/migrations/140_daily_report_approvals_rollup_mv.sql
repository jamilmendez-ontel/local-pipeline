-- 140: Concurrency/perf hardening for the Leads' Approval Cockpit.
--
-- Problem: analytics.v_daily_report_approvals re-aggregated the FULL
-- stg_daily_report_hours (18 MB) and stg_daily_report_attendance (9.8 MB) tables
-- with two HashAggregate seq-scans on EVERY call (every screen load, filter,
-- page, refresh). Measured ~258 ms / 5,513 shared buffers per page. With several
-- leads working the queue at once that full-table recompute is the bottleneck.
--
-- Fix: materialize ONLY the stable per-task rollup (total_hours, req_count,
-- first_clock_in, attendance user_name) into analytics.mv_daily_report_task_rollup
-- and have the view join it. All time-sensitive fields (is_awaiting_approval,
-- pending_wait_days, task_status, approved_on) stay LIVE in the view, so the queue
-- is never stale; only the expensive stable aggregates are cached.
--
-- Verified before cutover (prod, MCP): rewritten view is byte-identical to the
-- live view (0 rows differ across all 22,204 rows, both directions); page query
-- drops 258 ms -> 14.5 ms and 5,513 -> 808 shared buffers. Applied to prod 2026-07-01.
--
-- Refresh: pg_cron every 5 min (matches the daily-reports pipeline cadence), same
-- REFRESH ... CONCURRENTLY convention as the existing refresh_mv_* jobs. CONCURRENTLY
-- never blocks readers and requires the unique index below.

-- 1) Rollup MV: hrs + tmr aggregates collapsed to one row per task_did.
CREATE MATERIALIZED VIEW IF NOT EXISTS analytics.mv_daily_report_task_rollup AS
SELECT
  COALESCE(h.task_did, a.task_did)      AS task_did,
  h.total_hours,
  h.req_count,
  a.first_clock_in,
  a.attendance_user_name
FROM (
  SELECT task_did, sum(hours_worked) AS total_hours, count(*) AS req_count
  FROM data_staging.stg_daily_report_hours
  GROUP BY task_did
) h
FULL OUTER JOIN (
  SELECT task_did, min(timer_start) AS first_clock_in, max(user_name) AS attendance_user_name
  FROM data_staging.stg_daily_report_attendance
  GROUP BY task_did
) a ON h.task_did = a.task_did;

-- Required for REFRESH MATERIALIZED VIEW CONCURRENTLY.
CREATE UNIQUE INDEX IF NOT EXISTS mv_daily_report_task_rollup_pk
  ON analytics.mv_daily_report_task_rollup (task_did);

-- 2) Rewrite the serving view to join the rollup instead of re-aggregating.
--    Column list/order is unchanged from migration 123 (CREATE OR REPLACE contract),
--    so every dependent object (v_daily_report_approver_stats, the app) is unaffected.
CREATE OR REPLACE VIEW analytics.v_daily_report_approvals AS
 WITH today_et AS (
         SELECT (now() AT TIME ZONE 'America/New_York'::text)::date AS d
        )
 SELECT t.emp_id,
    COALESCE(
        e.full_name,
        CASE WHEN t.asset_name IS NOT NULL AND t.emp_id IS NOT NULL
                  AND right(t.asset_name, length(t.emp_id) + 1) = ('_' || t.emp_id)
             THEN left(t.asset_name, length(t.asset_name) - length(t.emp_id) - 1)
             ELSE NULL END,
        r.attendance_user_name
    ) AS employee_name,
    e.nickname, e.email, e."position", e.carrier, e.carrier_group, e.cluster,
    e.division, e.sub_division, e.employment_status,
    t.work_date, t.task_did, t.task_status, t.asset_name, t.milestone,
    COALESCE(r.req_count, 0::bigint) AS req_count,
    r.total_hours,
    (r.first_clock_in AT TIME ZONE 'America/New_York'::text) AS clock_in_et,
    t.assigned_approver,
    (t.submitted_on AT TIME ZONE 'America/New_York'::text) AS submitted_on_et,
    (t.approved_on AT TIME ZONE 'America/New_York'::text) AS approved_on_et,
    t.approved_by,
    t.task_status = 'submitted'::text AND t.approved_on IS NULL AS is_awaiting_approval,
        CASE WHEN t.task_status = 'submitted'::text AND t.approved_on IS NULL
             THEN ((SELECT today_et.d FROM today_et)) - (t.submitted_on AT TIME ZONE 'America/New_York'::text)::date
             ELSE NULL::integer END AS pending_wait_days,
        CASE WHEN t.approved_on IS NOT NULL AND t.submitted_on IS NOT NULL
             THEN (t.approved_on AT TIME ZONE 'America/New_York'::text)::date - (t.submitted_on AT TIME ZONE 'America/New_York'::text)::date
             ELSE NULL::integer END AS approval_latency_days,
    t.task_status = 'submitted'::text AND t.approved_on IS NULL AND t.assigned_approver IS NULL AS no_approver_flag
   FROM data_staging.stg_daily_reports t
     LEFT JOIN analytics.mv_daily_report_task_rollup r ON t.task_did = r.task_did
     LEFT JOIN LATERAL ( SELECT re.full_name, re.nickname, re.email, re."position", re.carrier,
            re.carrier_group, re.cluster, re.division, re.sub_division, re.employment_status
           FROM reference.ref_employees re
          WHERE re.emp_id = t.emp_id AND re.effective_date <= COALESCE(t.work_date, CURRENT_DATE)
          ORDER BY re.effective_date DESC LIMIT 1) e ON true
  WHERE t.work_date IS NOT NULL AND t.work_date <= ((SELECT today_et.d FROM today_et));

-- 3) Keep the rollup fresh. cron.schedule(name, ...) upserts by job name, so this
--    is safe to re-run. Every 5 min to match the daily-reports pipeline.
SELECT cron.schedule(
  'refresh_mv_daily_report_task_rollup',
  '*/5 * * * *',
  'REFRESH MATERIALIZED VIEW CONCURRENTLY analytics.mv_daily_report_task_rollup'
);

-- 4) Semantic-layer metadata for the new MV (DATABASE_ARCHITECTURE standard).
INSERT INTO agent.schema_metadata (schema_name, table_name, description, business_context, related_tables)
VALUES
  ('analytics','mv_daily_report_task_rollup',
   'Per-daily-report (task_did grain) rollup of total hours, requirement count, first clock-in, and attendance user name. Precomputed cache for the approval cockpit view.',
   'Performance cache behind analytics.v_daily_report_approvals: collapses the full stg_daily_report_hours and stg_daily_report_attendance aggregations into one indexed row per task so the leads approval queue serves in ~15 ms instead of ~260 ms under concurrent use. Refreshed every 5 min via pg_cron.',
   ARRAY['data_staging.stg_daily_report_hours','data_staging.stg_daily_report_attendance'])
ON CONFLICT DO NOTHING;
