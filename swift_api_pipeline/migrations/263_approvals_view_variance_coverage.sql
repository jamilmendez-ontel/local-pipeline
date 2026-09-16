-- 263: v_daily_report_approvals gains variance_hours + coverage_pct.
-- (Jamil 2026-09-15: "in DR Approval page we will add the variance column we
-- have in DR Monitoring".)
--
-- DR Monitoring's Variance cell reads variance_hours / coverage_pct from
-- mv_hr_report_review. DR Approval (browse) reads v_daily_report_approvals,
-- which until now exposed only the ROUNDED timed_hours, so a client-side
-- variance would drift 0.1 h / 1 pt from Monitoring on quarter-hour ties.
-- This migration adds the two columns to the approvals view with the SAME
-- expressions the monitoring MV uses (migration 223 lines 121-129), fed by the
-- raw timer minutes, so the two pages agree row for row.
--
-- Definitions (unchanged from 223; stated net of the 1 h break, floored at 0):
--   variance_hours = round(GREATEST(total_hours - 1, 0) - union_min / 60, 1)
--                    NULL when no timer rollup row, an open timer, or no stated hours
--   coverage_pct   = round(100 * union_min / 60 / GREATEST(total_hours - 1, 0), 0)
--                    NULL as above, or when the net stated hours are 0
--
-- Mechanics: CREATE OR REPLACE with the two columns APPENDED (Postgres refuses
-- to reorder/retype existing view columns; dependents mv_hr_report_review,
-- v_approver_options, v_daily_report_approver_stats and the 8 plpgsql readers
-- keep working untouched). The inner subquery carries tm.union_min out as
-- timer_union_min; the outer SELECT does not expose it. Body otherwise equals
-- pg_get_viewdef as of 261. Rollback at the bottom.

SET LOCAL lock_timeout = '15s';

CREATE OR REPLACE VIEW analytics.v_daily_report_approvals AS
 SELECT b.emp_id,
    b.employee_name,
    b.nickname,
    b.email,
    b."position",
    b.carrier,
    b.carrier_group,
    b.cluster,
    b.division,
    b.sub_division,
    b.employment_status,
    b.work_date,
    b.task_did,
    b.task_status,
    b.asset_name,
    b.milestone,
    b.req_count,
    b.total_hours,
    b.clock_in_et,
    b.assigned_approver,
    b.submitted_on_et,
    b.approved_on_et,
    b.approved_by,
    b.is_awaiting_approval,
    b.pending_wait_days,
    b.approval_latency_days,
    b.no_approver_flag,
    b.shift_time_in_pht,
    b.clock_in_late_minutes,
    b.timed_hours,
    b.open_timer_count,
    b.has_timer_history,
    b.work_dow,
    COALESCE(b.clock_in_late_minutes > 30 AND b.task_status IS DISTINCT FROM 'cancelled'::text AND (b.work_dow <> ALL (ARRAY[0, 6])) AND NOT (EXISTS ( SELECT 1
           FROM data_staging.stg_daily_report_hours h
          WHERE h.task_did = b.task_did AND h.work_description ~* '^\s*007\s*-?\s*UT\M'::text)), false) AS is_tardy,
    -- 263: same expressions as mv_hr_report_review (223), raw minutes in.
        CASE
            WHEN b.timed_hours IS NOT NULL AND b.open_timer_count = 0 AND b.total_hours IS NOT NULL
            THEN round(GREATEST(b.total_hours - 1::numeric, 0::numeric) - b.timer_union_min / 60.0, 1)
            ELSE NULL::numeric
        END AS variance_hours,
        CASE
            WHEN b.timed_hours IS NOT NULL AND b.open_timer_count = 0 AND b.total_hours IS NOT NULL
                 AND GREATEST(b.total_hours - 1::numeric, 0::numeric) > 0::numeric
            THEN round(100.0 * b.timer_union_min / 60.0 / GREATEST(b.total_hours - 1::numeric, 0::numeric), 0)
            ELSE NULL::numeric
        END AS coverage_pct
   FROM ( WITH today_et AS (
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
                CASE
                    WHEN t.task_status = 'cancelled'::text THEN 0::numeric
                    ELSE ( SELECT
                            CASE
                                WHEN count(*) = 0 THEN NULL::numeric
                                ELSE COALESCE(sum(h.hours_worked) FILTER (WHERE h.req_status IS DISTINCT FROM 'cancelled'::text), 0::numeric)
                            END AS "coalesce"
                       FROM data_staging.stg_daily_report_hours h
                      WHERE h.task_did = t.task_did)
                END AS total_hours,
            (r.first_clock_in AT TIME ZONE 'America/New_York'::text) AS clock_in_et,
            t.assigned_approver,
            (t.submitted_on AT TIME ZONE 'America/New_York'::text) AS submitted_on_et,
            (COALESCE(t.approved_on,
                CASE
                    WHEN la.swift_status = 'approved'::text THEN la.approved_at
                    ELSE NULL::timestamp with time zone
                END) AT TIME ZONE 'America/New_York'::text) AS approved_on_et,
            COALESCE(t.approved_by,
                CASE
                    WHEN la.task_did IS NOT NULL AND la.swift_status = 'approved'::text THEN COALESCE(appr.full_name, la.approver_email)
                    ELSE NULL::text
                END) AS approved_by,
            t.task_status = 'submitted'::text AND t.approved_on IS NULL AND la.task_did IS NULL AS is_awaiting_approval,
                CASE
                    WHEN t.task_status = 'submitted'::text AND t.approved_on IS NULL AND la.task_did IS NULL THEN (( SELECT today_et.d
                       FROM today_et)) - (t.submitted_on AT TIME ZONE 'America/New_York'::text)::date
                    ELSE NULL::integer
                END AS pending_wait_days,
                CASE
                    WHEN COALESCE(t.approved_on,
                    CASE
                        WHEN la.swift_status = 'approved'::text THEN la.approved_at
                        ELSE NULL::timestamp with time zone
                    END) IS NOT NULL AND t.submitted_on IS NOT NULL THEN (COALESCE(t.approved_on,
                    CASE
                        WHEN la.swift_status = 'approved'::text THEN la.approved_at
                        ELSE NULL::timestamp with time zone
                    END) AT TIME ZONE 'America/New_York'::text)::date - (t.submitted_on AT TIME ZONE 'America/New_York'::text)::date
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
            th.person_key IS NOT NULL AS has_timer_history,
            EXTRACT(dow FROM t.work_date)::smallint AS work_dow,
            -- 263: raw timer minutes for the outer variance/coverage math (not exposed).
            tm.union_min AS timer_union_min
           FROM data_staging.stg_daily_reports t
             LEFT JOIN analytics.mv_daily_report_task_rollup r ON t.task_did = r.task_did
             LEFT JOIN LATERAL ( SELECT l.task_did,
                    l.approver_email,
                    l.swift_status,
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
                 LIMIT 1) appr ON la.task_did IS NOT NULL AND la.swift_status = 'approved'::text
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
                   FROM today_pht))) b;

COMMENT ON VIEW analytics.v_daily_report_approvals IS
  'Daily-report approval serving view over stg_daily_reports + mv_daily_report_task_rollup. total_hours (migrations 260/261): 0 for a cancelled task or when every requirement is cancelled; otherwise the sum of the non-cancelled requirements; NULL only when the report has no requirement rows yet. req_count still counts every requirement. is_tardy per 215/230. variance_hours / coverage_pct (263): same expressions as mv_hr_report_review (stated net of the 1h break vs raw timer minutes; NULL with an open timer, no timer rollup row, or no stated hours) so DR Approval and DR Monitoring agree.';

-- Verification (run after apply; expect 0 mismatches):
--   SELECT count(*) FILTER (WHERE a.variance_hours IS DISTINCT FROM m.variance_hours
--                              OR a.coverage_pct   IS DISTINCT FROM m.coverage_pct) AS mismatches,
--          count(*) AS compared
--   FROM analytics.v_daily_report_approvals a
--   JOIN analytics.mv_hr_report_review m USING (task_did)
--   WHERE a.work_date >= current_date - 30;
--   (mv_hr_report_review refreshes every 5 min, so a handful of rows can differ
--   while a timer is running; re-run after a refresh if the count is not 0.)

-- Rollback (NEVER DROP the view: mv_hr_report_review, v_approver_options and
-- v_daily_report_approver_stats depend on it). Postgres cannot drop view
-- columns with CREATE OR REPLACE, so the honest rollback is to leave the two
-- columns in place and stop reading them: they are pure expressions over
-- columns the view already had, add no join, and cost nothing when unselected.
