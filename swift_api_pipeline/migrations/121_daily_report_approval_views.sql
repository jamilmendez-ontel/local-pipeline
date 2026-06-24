-- 121: Leads' Approval Cockpit serving views.
-- v_daily_report_approvals: one row per daily report (task grain), children aggregated.
-- v_daily_report_approver_stats: per-individual historical approval latency (YTD).

CREATE OR REPLACE VIEW analytics.v_daily_report_approvals AS
WITH today_et AS (
  SELECT (now() AT TIME ZONE 'America/New_York')::date AS d
),
hrs AS (
  SELECT task_did, sum(hours_worked) AS total_hours, count(*) AS req_count
  FROM data_staging.stg_daily_report_hours
  GROUP BY task_did
),
tmr AS (
  SELECT task_did, min(timer_start) AS first_clock_in
  FROM data_staging.stg_daily_report_attendance
  GROUP BY task_did
)
SELECT
  t.emp_id,
  e.full_name              AS employee_name,
  e.nickname,
  e.email,
  e."position",
  e.carrier,
  e.carrier_group,
  e.cluster,
  e.division,
  e.sub_division,
  e.employment_status,
  t.work_date,
  t.task_did,
  t.task_status,
  t.asset_name,
  t.milestone,
  COALESCE(h.req_count, 0) AS req_count,
  h.total_hours,
  (tmr.first_clock_in AT TIME ZONE 'America/New_York') AS clock_in_et,
  t.assigned_approver,
  (t.submitted_on AT TIME ZONE 'America/New_York')     AS submitted_on_et,
  (t.approved_on  AT TIME ZONE 'America/New_York')     AS approved_on_et,
  t.approved_by,
  (t.task_status = 'submitted' AND t.approved_on IS NULL) AS is_awaiting_approval,
  CASE WHEN t.task_status = 'submitted' AND t.approved_on IS NULL
       THEN (SELECT d FROM today_et) - (t.submitted_on AT TIME ZONE 'America/New_York')::date
       ELSE NULL END AS pending_wait_days,
  CASE WHEN t.approved_on IS NOT NULL AND t.submitted_on IS NOT NULL
       THEN (t.approved_on AT TIME ZONE 'America/New_York')::date
            - (t.submitted_on AT TIME ZONE 'America/New_York')::date
       ELSE NULL END AS approval_latency_days,
  (t.task_status = 'submitted' AND t.approved_on IS NULL AND t.assigned_approver IS NULL) AS no_approver_flag
FROM data_staging.stg_daily_reports t
LEFT JOIN hrs h   ON t.task_did = h.task_did
LEFT JOIN tmr     ON t.task_did = tmr.task_did
LEFT JOIN LATERAL (
  SELECT re.full_name, re.nickname, re.email, re."position", re.carrier, re.carrier_group,
         re.cluster, re.division, re.sub_division, re.employment_status
  FROM reference.ref_employees re
  WHERE re.emp_id = t.emp_id AND re.effective_date <= COALESCE(t.work_date, CURRENT_DATE)
  ORDER BY re.effective_date DESC
  LIMIT 1
) e ON true
WHERE t.work_date IS NOT NULL
  AND t.work_date <= (SELECT d FROM today_et);

CREATE OR REPLACE VIEW analytics.v_daily_report_approver_stats AS
SELECT
  approved_by AS approver,
  count(*)                                                              AS approved_count,
  round(avg(approval_latency_days)::numeric, 1)                         AS avg_latency_days,
  percentile_cont(0.5) WITHIN GROUP (ORDER BY approval_latency_days)    AS p50_latency_days,
  percentile_cont(0.9) WITHIN GROUP (ORDER BY approval_latency_days)    AS p90_latency_days,
  max(approval_latency_days)                                            AS max_latency_days
FROM analytics.v_daily_report_approvals
WHERE task_status = 'approved'
  AND approval_latency_days IS NOT NULL
  AND approved_by IS NOT NULL
GROUP BY approved_by;

INSERT INTO agent.schema_metadata (schema_name, table_name, description, business_context, related_tables)
VALUES
  ('analytics','v_daily_report_approvals',
   'One row per employee daily report (task grain). Approval lifecycle, aging, and team context for the leads approval cockpit.',
   'Drives the HR approval cockpit: awaiting-approval queue, days waiting, assigned approver group, and historical latency. Excludes future-dated empty shells.',
   ARRAY['data_staging.stg_daily_reports','data_staging.stg_daily_report_hours','data_staging.stg_daily_report_attendance','reference.ref_employees']),
  ('analytics','v_daily_report_approver_stats',
   'Per-individual historical daily-report approval latency (count, avg, p50, p90, max), year to date.',
   'Approver scorecard for the HR approval cockpit. Latency measured submitted to approved.',
   ARRAY['analytics.v_daily_report_approvals'])
ON CONFLICT DO NOTHING;
