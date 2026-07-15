-- 183: DR Approval two-hours columns.
-- Append timer-rollup columns (timed_hours, open_timer_count,
-- has_timer_history) to analytics.v_daily_report_approvals so the DR Approval
-- browse table can show "Stated hrs" + "Timer hrs" exactly like DR Monitoring.
-- Semantics COPIED from analytics.v_hr_report_review (migrations 145/146/170):
-- analytics.mv_timer_day_rollup keyed by (person_key = emp_id, work_day =
-- work_date); timed_hours = union_min/60 rounded 1dp; has_timer_history via
-- the person-level lateral probe.
--
-- CREATE OR REPLACE VIEW appends the new columns at the END, so the existing
-- column order/types are unchanged and dependents keep working untouched
-- (checked via pg_depend 2026-07-15: v_hr_report_review,
-- v_daily_report_approver_stats, plus the approval_queue_summary /
-- dr_attachment_counts / hr_review_* RPCs which reference columns by name).
-- v_hr_report_review keeps its own tm join; mv_timer_day_rollup has the
-- unique (person_key, work_day) index (REFRESH CONCURRENTLY requirement), so
-- the planner can eliminate the duplicate join where the columns go unused.
-- Everything below the two new join lines and three new output columns is a
-- byte-faithful transcription of the live view (pg_get_viewdef 2026-07-15).

CREATE OR REPLACE VIEW analytics.v_daily_report_approvals AS
 WITH today_et AS (
         SELECT (now() AT TIME ZONE 'America/New_York'::text)::date AS d
        ), today_pht AS (
         SELECT (now() AT TIME ZONE 'Asia/Manila'::text)::date AS d
        )
 SELECT t.emp_id,
    COALESCE(
        CASE
            WHEN t.asset_name IS NOT NULL AND t.emp_id IS NOT NULL AND "right"(t.asset_name, length(t.emp_id) + 1) = ('_'::text || t.emp_id) THEN "left"(t.asset_name, length(t.asset_name) - length(t.emp_id) - 1)
            ELSE NULL::text
        END, e.full_name, r.attendance_user_name) AS employee_name,
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
        CASE
            WHEN t.task_status = 'approved'::text THEN t.task_status
            WHEN la.task_did IS NOT NULL THEN 'approved'::text
            ELSE t.task_status
        END AS task_status,
    t.asset_name,
    t.milestone,
    COALESCE(r.req_count, 0::bigint) AS req_count,
    r.total_hours,
    (r.first_clock_in AT TIME ZONE 'America/New_York'::text) AS clock_in_et,
    t.assigned_approver,
    (t.submitted_on AT TIME ZONE 'America/New_York'::text) AS submitted_on_et,
    (COALESCE(t.approved_on, la.approved_at) AT TIME ZONE 'America/New_York'::text) AS approved_on_et,
    COALESCE(t.approved_by,
        CASE
            WHEN la.task_did IS NOT NULL THEN COALESCE(appr.full_name, la.approver_email)
            ELSE NULL::text
        END) AS approved_by,
    t.task_status = 'submitted'::text AND t.approved_on IS NULL AND la.task_did IS NULL AS is_awaiting_approval,
        CASE
            WHEN t.task_status = 'submitted'::text AND t.approved_on IS NULL AND la.task_did IS NULL THEN (( SELECT today_et.d
               FROM today_et)) - (t.submitted_on AT TIME ZONE 'America/New_York'::text)::date
            ELSE NULL::integer
        END AS pending_wait_days,
        CASE
            WHEN COALESCE(t.approved_on, la.approved_at) IS NOT NULL AND t.submitted_on IS NOT NULL THEN (COALESCE(t.approved_on, la.approved_at) AT TIME ZONE 'America/New_York'::text)::date - (t.submitted_on AT TIME ZONE 'America/New_York'::text)::date
            ELSE NULL::integer
        END AS approval_latency_days,
    t.task_status = 'submitted'::text AND t.approved_on IS NULL AND la.task_did IS NULL AND t.assigned_approver IS NULL AS no_approver_flag,
    e.shift_time_in_pht,
        CASE
            WHEN r.first_clock_in IS NOT NULL AND e.shift_time_in_pht ~* '^\s*\d{1,2}(:\d{2})?\s*(AM|PM)\s*$'::text THEN mod(floor(EXTRACT(epoch FROM (r.first_clock_in AT TIME ZONE 'Asia/Manila'::text)::time without time zone - e.shift_time_in_pht::time without time zone) / 60::numeric)::integer + 2160, 1440) - 720
            ELSE NULL::integer
        END AS clock_in_late_minutes,
        CASE
            WHEN tm.person_key IS NOT NULL THEN round(tm.union_min / 60.0, 1)
            ELSE NULL::numeric
        END AS timed_hours,
    COALESCE(tm.open_count, 0::bigint) AS open_timer_count,
    th.person_key IS NOT NULL AS has_timer_history
   FROM data_staging.stg_daily_reports t
     LEFT JOIN analytics.mv_daily_report_task_rollup r ON t.task_did = r.task_did
     LEFT JOIN LATERAL ( SELECT l.task_did,
            l.approver_email,
            l.approved_at
           FROM app_hr.report_approval_log l
          WHERE l.task_did = t.task_did AND l.ok AND l.approved_at >= (now() - '30 days'::interval)
         LIMIT 1) la ON true
     LEFT JOIN LATERAL ( SELECT re2.full_name
           FROM reference.ref_employees re2
          WHERE re2.emp_id = (( SELECT ea.emp_id
                   FROM reference.ref_employee_emails ea
                  WHERE ea.email = lower(la.approver_email)
                  ORDER BY ea.last_seen DESC
                 LIMIT 1)) OR lower(re2.email) = lower(la.approver_email)
          ORDER BY re2.effective_date DESC
         LIMIT 1) appr ON la.task_did IS NOT NULL
     LEFT JOIN LATERAL ( SELECT re.full_name,
            re.nickname,
            re.email,
            re."position",
            re.carrier,
            re.carrier_group,
            re.cluster,
            re.division,
            re.sub_division,
            re.employment_status,
            re.shift_time_in_pht
           FROM reference.ref_employees re
          WHERE re.emp_id = t.emp_id AND re.effective_date <= COALESCE(t.work_date, CURRENT_DATE)
          ORDER BY re.effective_date DESC
         LIMIT 1) e ON true
     LEFT JOIN analytics.mv_timer_day_rollup tm ON tm.person_key = t.emp_id AND tm.work_day = t.work_date
     LEFT JOIN LATERAL ( SELECT m2.person_key
           FROM analytics.mv_timer_day_rollup m2
          WHERE m2.person_key = t.emp_id
         LIMIT 1) th ON true
  WHERE t.work_date IS NOT NULL AND t.work_date <= (( SELECT today_pht.d
           FROM today_pht));
