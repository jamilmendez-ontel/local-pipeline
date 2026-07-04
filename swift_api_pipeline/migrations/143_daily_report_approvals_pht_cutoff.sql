-- 143: Window the daily-report serving view's "today" cutoff on Manila time.
--
-- Problem: analytics.v_daily_report_approvals ends with
--   WHERE t.work_date <= (now() AT TIME ZONE 'America/New_York')::date
-- The cutoff exists only to hide FUTURE-scheduled tasks (work_date runs out to
-- 2026-12-31 in staging). But "today" was Eastern, and the app's users are mostly
-- in the Philippines (UTC+8, ~12-13h ahead of ET). So the current PH work day's
-- rows -- which are already ingested and correctly dated in PHT -- stay hidden
-- until ET itself rolls over, i.e. until ~12:00 noon PHT. A PH user who times in
-- at 10:02 AM sees no data for "today" for half the day.
--
-- Fix: switch ONLY the cutoff to Asia/Manila, so the current PH calendar day is
-- visible as soon as it lands. This does not change history and does not touch the
-- SLA/aging math: pending_wait_days deliberately stays on America/New_York (see
-- today_et below) so payroll-adjacent approval-latency counting is unchanged. This
-- is the "cutoff only" scope; any move of SLA counting to PHT is a separate call.
--
-- Column list/order is unchanged from migration 140 (CREATE OR REPLACE contract),
-- so every dependent object (v_daily_report_approver_stats, the scorecard RPCs,
-- the app, DARA) is unaffected. Rollback: re-run migration 140.

CREATE OR REPLACE VIEW analytics.v_daily_report_approvals AS
 WITH today_et AS (
         SELECT (now() AT TIME ZONE 'America/New_York'::text)::date AS d
        ),
      today_pht AS (
         SELECT (now() AT TIME ZONE 'Asia/Manila'::text)::date AS d
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
  WHERE t.work_date IS NOT NULL AND t.work_date <= ((SELECT today_pht.d FROM today_pht));
