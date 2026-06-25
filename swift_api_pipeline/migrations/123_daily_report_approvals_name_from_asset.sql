-- 123: recover employee_name from the daily-report asset, which is named
-- "FullName_<emp_id>", when the employee is not in reference.ref_employees.
-- Only parses when the asset actually ends with "_<emp_id>" (so real node/site
-- assets are never misparsed); falls back to the timer name last. With this,
-- 0 of 21k rows show a bare emp_id. Applied to prod via MCP 2026-06-25.
CREATE OR REPLACE VIEW analytics.v_daily_report_approvals AS
 WITH today_et AS (
         SELECT (now() AT TIME ZONE 'America/New_York'::text)::date AS d
        ), hrs AS (
         SELECT stg_daily_report_hours.task_did,
            sum(stg_daily_report_hours.hours_worked) AS total_hours,
            count(*) AS req_count
           FROM data_staging.stg_daily_report_hours
          GROUP BY stg_daily_report_hours.task_did
        ), tmr AS (
         SELECT stg_daily_report_attendance.task_did,
            min(stg_daily_report_attendance.timer_start) AS first_clock_in,
            max(stg_daily_report_attendance.user_name) AS user_name
           FROM data_staging.stg_daily_report_attendance
          GROUP BY stg_daily_report_attendance.task_did
        )
 SELECT t.emp_id,
    COALESCE(
        e.full_name,
        CASE WHEN t.asset_name IS NOT NULL AND t.emp_id IS NOT NULL
                  AND right(t.asset_name, length(t.emp_id) + 1) = ('_' || t.emp_id)
             THEN left(t.asset_name, length(t.asset_name) - length(t.emp_id) - 1)
             ELSE NULL END,
        tmr.user_name
    ) AS employee_name,
    e.nickname, e.email, e."position", e.carrier, e.carrier_group, e.cluster,
    e.division, e.sub_division, e.employment_status,
    t.work_date, t.task_did, t.task_status, t.asset_name, t.milestone,
    COALESCE(h.req_count, 0::bigint) AS req_count,
    h.total_hours,
    (tmr.first_clock_in AT TIME ZONE 'America/New_York'::text) AS clock_in_et,
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
     LEFT JOIN hrs h ON t.task_did = h.task_did
     LEFT JOIN tmr ON t.task_did = tmr.task_did
     LEFT JOIN LATERAL ( SELECT re.full_name, re.nickname, re.email, re."position", re.carrier,
            re.carrier_group, re.cluster, re.division, re.sub_division, re.employment_status
           FROM reference.ref_employees re
          WHERE re.emp_id = t.emp_id AND re.effective_date <= COALESCE(t.work_date, CURRENT_DATE)
          ORDER BY re.effective_date DESC LIMIT 1) e ON true
  WHERE t.work_date IS NOT NULL AND t.work_date <= ((SELECT today_et.d FROM today_et));
