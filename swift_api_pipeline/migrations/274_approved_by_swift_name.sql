-- 274: v_daily_report_approvals.approved_by shows the approver's Swift name on
-- reports approved inside DRMC. (Jamil 2026-09-29: "in the approved by the name
-- there is still the complete name not the swift name. it should be the swift
-- name".)
--
-- approved_by has two sources:
--   1. stg_daily_reports.approved_by, Swift's own approvedBy.name, already the
--      short Swift name ("Coleen Clarita"). Unchanged.
--   2. The app overlay (156/184): a report approved in DRMC shows as approved
--      before the pipeline pulls Swift's value. That branch resolved the
--      approver's email to reference.ref_employees.full_name, the legal name
--      ("Coleen Panganiban Clarita"), so the cell read the complete name until
--      the next pull replaced it.
--
-- Fix: branch 2 now resolves to the name on the approver's own latest daily
-- report (asset_name minus the "_<emp_id>" suffix), the same parse 163 uses for
-- employee_name and 188 exposes as v_employee_directory.report_display_name.
-- Falls back to full_name (approver with no parseable report), then to the
-- email (approver not in the roster), as before.
--
-- Checked 2026-09-29 against every approver who has used DRMC (18): the report
-- name equals the name Swift writes for 18 of 18 (Roy Riotoc differs only by
-- Swift's double space). reference.ref_swift_users was considered and rejected:
-- it holds 13 of those 18.
--
-- Mechanics: CREATE OR REPLACE, column list and types unchanged, so dependents
-- (mv_hr_report_review, v_approver_options, v_daily_report_approver_stats and
-- the 8 SQL function readers) are untouched. The extra lookup sits inside the
-- appr lateral, which returns a row for every report that has an approved
-- DRMC log row in the last 30 days (1,638 on 2026-09-29), whether or not the
-- overlay value is the one displayed. The planner memoizes the lateral per
-- approver email, so the lookup ran 12 to 17 times per read; each run reads the
-- approver's own reports (about 365 rows) through idx_stg_dr_tasks_emp in
-- under 1 ms. Without the memoize node a 30-day read would gain about 1.2 s,
-- still under the 8 s PostgREST timeout. Body otherwise equals pg_get_viewdef
-- as of 263. Rollback at the bottom.
--
-- STATUS: APPLIED 2026-09-29 07:25:23 ET via MCP apply_migration (recorded as
--   20260929112523 274_approved_by_swift_name).
-- BEFORE: 1 overlay-sourced row in the view, reading "Roel Rivera Longcop".
-- TESTED on a temporary copy (analytics.v_daily_report_approvals_test_274,
--   dropped before the apply): 33,184 rows compared with the live view, 0 rows
--   missing, 0 differences in any other column, 1 approved_by change
--   ("Roel Rivera Longcop" -> "Roel Longcop"). 14-day read of task_did and
--   approved_by only, warm cache: 29.6 ms before, 37.7 ms after.
-- VERIFIED after the apply: view 33,184 rows; verification query below 0 rows;
--   the 3 dependents present, mv_hr_report_review populated; grants unchanged.
-- INDEPENDENT CHECK (pre-merge review, read-only, same day): live body equals
--   this file (comments and whitespace stripped); 36 columns, same order and
--   types; 3 dependents and 8 function readers all select; the 5-minute
--   refresh job ran 7 of 7 times after the apply; anon and authenticated hold
--   no privilege. Full-row reads: 14 days 115 ms warm and 604 ms cold, 60 days
--   438 ms, whole view 1,218 ms, one report 2.4 ms. All 18 DRMC approvers
--   resolve through the report name; 17 equal Swift's name exactly, Roy Riotoc
--   differs only by the double space Swift holds ("Roy  Riotoc").

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
                    -- 274: appr.display_name, was appr.full_name.
                    WHEN la.task_did IS NOT NULL AND la.swift_status = 'approved'::text THEN COALESCE(appr.display_name, la.approver_email)
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
             -- 274: the approver's Swift name (the name on their own latest daily
             -- report, same parse as employee_name and as
             -- v_employee_directory.report_display_name), roster full_name only
             -- when they have no parseable report.
             LEFT JOIN LATERAL ( SELECT COALESCE(( SELECT "left"(dr.asset_name, length(dr.asset_name) - length(dr.emp_id) - 1)
                           FROM data_staging.stg_daily_reports dr
                          WHERE dr.emp_id = re2.emp_id AND dr.asset_name IS NOT NULL AND "right"(dr.asset_name, length(dr.emp_id) + 1) = ('_'::text || dr.emp_id) AND dr.work_date IS NOT NULL
                          ORDER BY dr.work_date DESC
                         LIMIT 1), re2.full_name) AS display_name
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
  'Daily-report approval serving view over stg_daily_reports + mv_daily_report_task_rollup. total_hours (migrations 260/261): 0 for a cancelled task or when every requirement is cancelled; otherwise the sum of the non-cancelled requirements; NULL only when the report has no requirement rows yet. req_count still counts every requirement. is_tardy per 215/230. variance_hours / coverage_pct (263): same expressions as mv_hr_report_review (stated net of the 1h break vs raw timer minutes; NULL with an open timer, no timer rollup row, or no stated hours) so DR Approval and DR Monitoring agree. approved_by (274): Swift''s approver name; for a report approved in DRMC and not yet pulled, the approver''s own report name, then roster full_name, then email.';

-- Verification (run after apply; expect 0 rows). A row means a report still
-- shows the legal name although the approver has a different report name. An
-- approver with no report name is skipped: full_name is the intended fallback.
--   SELECT DISTINCT v.task_did, v.approved_by, d.report_display_name
--   FROM analytics.v_daily_report_approvals v
--   JOIN app_hr.report_approval_log l USING (task_did)
--   JOIN analytics.v_employee_directory d ON lower(d.email) = lower(l.approver_email)
--   WHERE l.ok AND v.approved_by = d.full_name
--     AND d.report_display_name IS NOT NULL
--     AND d.report_display_name <> d.full_name;
--
-- Rollback: CREATE OR REPLACE the view with this file's body after reverting
-- BOTH edits marked "274" (revert one without the other and the statement
-- fails, because approved_by and the lateral must name the same column):
--   1. in the approved_by expression, COALESCE(appr.display_name, la.approver_email)
--      goes back to COALESCE(appr.full_name, la.approver_email);
--   2. in the appr lateral, the select list
--      "SELECT COALESCE(( SELECT ... LIMIT 1), re2.full_name) AS display_name"
--      goes back to "SELECT re2.full_name".
-- Then run COMMENT ON VIEW with the text above minus its last sentence
-- ("approved_by (274): ..."). Never DROP the view: mv_hr_report_review and two
-- views depend on it.
