-- =============================================================================
-- 296_tardy_us_shift_dst.sql
-- DRMC tardy flag: US-hours roles keep their US clock time across the US DST change.
--
-- STATUS: NOT APPLIED (PR open). Apply through MCP apply_migration after Jamil's go.
--
-- WHY
--   * reference.ref_employees.shift_time_in_pht holds one PHT clock time per person, set while
--     the US was on daylight time (EDT, UTC-4). US-hours staff start at a fixed US Eastern time,
--     so after US DST ended (2026-11-01) their start moved one hour later in PHT, and every
--     on-time clock-in counted as ~60 min late.
--   * Measured 2026-11-25 (weekdays, Oct vs Nov 2-24): tardy 1.7% -> 50.6%; per-person median
--     clock-in moved +59 min; 56 of 96 people moved 45-75 min.
--   * Jamil 2026-11-25: Technical Analyst (TA), Technical Associate (TAS) and Project
--     Associate (PAS) roles are US-hours. Everyone else stays as is (PHT-anchored).
--
-- CHANGE (one expression, everything else = live definition md5 6642982a435534aa1f2596ea6d90225a)
--   clock_in_late_minutes compares against shift_time_in_pht + adj, where for those roles
--   adj = -4 h - (America/New_York UTC offset at noon on work_date) = 0 on EDT dates, +1 h on EST
--   dates; adj = 0 for every other role. Same columns and types, so CREATE OR REPLACE keeps
--   every dependent (mv_hr_report_review, v_hr_report_review, v_approver_options,
--   v_daily_report_approver_stats, v_schedule_roster_reconciliation, v_employee_directory,
--   v_employee_shifts). is_tardy follows automatically. mv_hr_report_review picks it up on its
--   next scheduled REFRESH.
--
-- DRY RUN (no DDL; same math applied to the live view's output, 2026-11-25)
--   TA/TAS/PA, Nov: tardy 70.0% -> 1.6%, median late +50 -> -10 min (Oct: -13). Oct unchanged.
--   Other roles: unchanged (14.1% Nov; QPI / Operations Manager / Creative Lead also moved ~1 h,
--   out of scope until Jamil says they are US-hours too).
--
-- PREFLIGHT
--   * pg_get_viewdef md5 must still be 6642982a435534aa1f2596ea6d90225a before applying
--     (regenerate with out/m296/gen.py if it changed).
--   * Run outside 09:30-10:30 UTC and other batch windows (2026-11-20 restart lesson).
--
-- ROLLBACK
--   Apply 296_tardy_us_shift_dst_ROLLBACK.sql (the exact pre-296 definition).
--
-- VERIFY AFTER
--   select md5(pg_get_viewdef('analytics.v_daily_report_approvals'::regclass));  -- new md5, record it
--   tardy % for TA/TAS/PA Nov 2-24 weekdays ~1.6%; Oct unchanged.
-- =============================================================================

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
    COALESCE(((b.clock_in_late_minutes > 30) AND (b.task_status IS DISTINCT FROM 'cancelled'::text) AND (b.work_dow <> ALL (ARRAY[0, 6])) AND (NOT (EXISTS ( SELECT 1
           FROM data_staging.stg_daily_report_hours h
          WHERE ((h.task_did = b.task_did) AND (h.work_description ~* '^\s*007\s*-?\s*UT\M'::text))))) AND (NOT b.onpoint_undertime)), false) AS is_tardy,
        CASE
            WHEN ((b.timed_hours IS NOT NULL) AND (b.open_timer_count = 0) AND (b.total_hours IS NOT NULL)) THEN round((GREATEST((b.total_hours - (1)::numeric), (0)::numeric) - (b.timer_union_min / 60.0)), 1)
            ELSE NULL::numeric
        END AS variance_hours,
        CASE
            WHEN ((b.timed_hours IS NOT NULL) AND (b.open_timer_count = 0) AND (b.total_hours IS NOT NULL) AND (GREATEST((b.total_hours - (1)::numeric), (0)::numeric) > (0)::numeric)) THEN round((((100.0 * b.timer_union_min) / 60.0) / GREATEST((b.total_hours - (1)::numeric), (0)::numeric)), 0)
            ELSE NULL::numeric
        END AS coverage_pct,
    b.source,
    b.first_clock_in,
    b.submitted_on,
    b.onpoint_undertime,
    b.onpoint_report_id
   FROM ( WITH today_et AS (
                 SELECT ((now() AT TIME ZONE 'America/New_York'::text))::date AS d
                ), today_pht AS (
                 SELECT ((now() AT TIME ZONE 'Asia/Manila'::text))::date AS d
                )
         SELECT t.emp_id,
            COALESCE(
                CASE
                    WHEN ((t.asset_name IS NOT NULL) AND (t.emp_id IS NOT NULL) AND ("right"(t.asset_name, (length(t.emp_id) + 1)) = ('_'::text || t.emp_id))) THEN "left"(t.asset_name, ((length(t.asset_name) - length(t.emp_id)) - 1))
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
                    WHEN (op.emp_id IS NOT NULL) THEN op.status
                    WHEN (t.task_status = 'approved'::text) THEN t.task_status
                    WHEN (la.task_did IS NOT NULL) THEN 'approved'::text
                    ELSE t.task_status
                END AS task_status,
            t.asset_name,
            t.milestone,
            COALESCE(r.req_count, (0)::bigint) AS req_count,
                CASE
                    WHEN (op.emp_id IS NOT NULL) THEN op.hours
                    WHEN (t.task_status = 'cancelled'::text) THEN (0)::numeric
                    ELSE ( SELECT
                            CASE
                                WHEN (count(*) = 0) THEN NULL::numeric
                                ELSE COALESCE(sum(h.hours_worked) FILTER (WHERE (h.req_status IS DISTINCT FROM 'cancelled'::text)), (0)::numeric)
                            END AS "coalesce"
                       FROM data_staging.stg_daily_report_hours h
                      WHERE (h.task_did = t.task_did))
                END AS total_hours,
            (eff.first_clock_in AT TIME ZONE 'America/New_York'::text) AS clock_in_et,
            t.assigned_approver,
            (eff.submitted_on AT TIME ZONE 'America/New_York'::text) AS submitted_on_et,
            (COALESCE(t.approved_on,
                CASE
                    WHEN (la.swift_status = 'approved'::text) THEN la.approved_at
                    ELSE NULL::timestamp with time zone
                END) AT TIME ZONE 'America/New_York'::text) AS approved_on_et,
            COALESCE(t.approved_by,
                CASE
                    WHEN ((la.task_did IS NOT NULL) AND (la.swift_status = 'approved'::text)) THEN COALESCE(appr.display_name, la.approver_email)
                    ELSE NULL::text
                END) AS approved_by,
            ((op.emp_id IS NULL) AND (t.task_status = 'submitted'::text) AND (t.approved_on IS NULL) AND (la.task_did IS NULL)) AS is_awaiting_approval,
                CASE
                    WHEN ((op.emp_id IS NULL) AND (t.task_status = 'submitted'::text) AND (t.approved_on IS NULL) AND (la.task_did IS NULL)) THEN (( SELECT today_et.d
                       FROM today_et) - ((eff.submitted_on AT TIME ZONE 'America/New_York'::text))::date)
                    ELSE NULL::integer
                END AS pending_wait_days,
                CASE
                    WHEN ((COALESCE(t.approved_on,
                    CASE
                        WHEN (la.swift_status = 'approved'::text) THEN la.approved_at
                        ELSE NULL::timestamp with time zone
                    END) IS NOT NULL) AND (eff.submitted_on IS NOT NULL)) THEN (((COALESCE(t.approved_on,
                    CASE
                        WHEN (la.swift_status = 'approved'::text) THEN la.approved_at
                        ELSE NULL::timestamp with time zone
                    END) AT TIME ZONE 'America/New_York'::text))::date - ((eff.submitted_on AT TIME ZONE 'America/New_York'::text))::date)
                    ELSE NULL::integer
                END AS approval_latency_days,
            ((op.emp_id IS NULL) AND (t.task_status = 'submitted'::text) AND (t.approved_on IS NULL) AND (la.task_did IS NULL) AND (t.assigned_approver IS NULL)) AS no_approver_flag,
            e.shift_time_in_pht,
                CASE
                    WHEN ((eff.first_clock_in IS NOT NULL) AND (e.shift_time_in_pht ~* '^\s*\d{1,2}(:\d{2})?\s*(AM|PM)\s*$'::text)) THEN (mod(((floor((EXTRACT(epoch FROM (((eff.first_clock_in AT TIME ZONE 'Asia/Manila'::text))::time without time zone - ((e.shift_time_in_pht)::time without time zone + CASE WHEN (e."position" ~* '^\s*(Technical Analyst|Technical Associate|Project Associate)\M'::text) THEN ('-04:00:00'::interval - ((t.work_date + '12:00:00'::time without time zone) - (((t.work_date + '12:00:00'::time without time zone) AT TIME ZONE 'America/New_York'::text) AT TIME ZONE 'UTC'::text))) ELSE '00:00:00'::interval END))) / (60)::numeric)))::integer + 2160), 1440) - 720)
                    ELSE NULL::integer
                END AS clock_in_late_minutes,
                CASE
                    WHEN (tm.person_key IS NOT NULL) THEN round((tm.union_min / 60.0), 1)
                    ELSE NULL::numeric
                END AS timed_hours,
            COALESCE(tm.open_count, (0)::bigint) AS open_timer_count,
            (th.person_key IS NOT NULL) AS has_timer_history,
            (EXTRACT(dow FROM t.work_date))::smallint AS work_dow,
            tm.union_min AS timer_union_min,
                CASE
                    WHEN (op.emp_id IS NOT NULL) THEN 'onpoint'::text
                    ELSE 'swift'::text
                END AS source,
            eff.first_clock_in,
            eff.submitted_on,
            COALESCE((op.notes ~* '^\s*007\s*-?\s*UT\M'::text), false) AS onpoint_undertime,
            op.report_id AS onpoint_report_id
           FROM ((((((((data_staging.stg_daily_reports t
             LEFT JOIN analytics.mv_daily_report_task_rollup r ON ((t.task_did = r.task_did)))
             LEFT JOIN analytics.v_onpoint_daily_reports op ON (((op.emp_id = t.emp_id) AND (op.work_date = t.work_date))))
             CROSS JOIN LATERAL ( SELECT
                        CASE
                            WHEN (op.emp_id IS NOT NULL) THEN COALESCE(op.time_in, r.first_clock_in)
                            ELSE r.first_clock_in
                        END AS first_clock_in,
                        CASE
                            WHEN (op.emp_id IS NOT NULL) THEN COALESCE(op.submitted_at, t.submitted_on)
                            ELSE t.submitted_on
                        END AS submitted_on) eff)
             LEFT JOIN LATERAL ( SELECT l.task_did,
                    l.approver_email,
                    l.swift_status,
                    l.approved_at
                   FROM app_hr.report_approval_log l
                  WHERE ((l.task_did = t.task_did) AND l.ok AND (l.approved_at >= (now() - '30 days'::interval)))
                 LIMIT 1) la ON (true))
             LEFT JOIN LATERAL ( SELECT COALESCE(( SELECT "left"(dr.asset_name, ((length(dr.asset_name) - length(dr.emp_id)) - 1)) AS "left"
                           FROM data_staging.stg_daily_reports dr
                          WHERE ((dr.emp_id = re2.emp_id) AND (dr.asset_name IS NOT NULL) AND ("right"(dr.asset_name, (length(dr.emp_id) + 1)) = ('_'::text || dr.emp_id)) AND (dr.work_date IS NOT NULL))
                          ORDER BY dr.work_date DESC
                         LIMIT 1), re2.full_name) AS display_name
                   FROM reference.ref_employees re2
                  WHERE ((re2.emp_id = ( SELECT ea.emp_id
                           FROM reference.ref_employee_emails ea
                          WHERE (ea.email = lower(la.approver_email))
                          ORDER BY ea.last_seen DESC
                         LIMIT 1)) OR (lower(re2.email) = lower(la.approver_email)))
                  ORDER BY re2.effective_date DESC
                 LIMIT 1) appr ON (((la.task_did IS NOT NULL) AND (la.swift_status = 'approved'::text))))
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
                  WHERE ((re.emp_id = t.emp_id) AND (re.effective_date <= COALESCE(t.work_date, CURRENT_DATE)))
                  ORDER BY re.effective_date DESC
                 LIMIT 1) e ON (true))
             LEFT JOIN analytics.mv_timer_day_rollup tm ON (((tm.person_key = t.emp_id) AND (tm.work_day = t.work_date))))
             LEFT JOIN LATERAL ( SELECT m2.person_key
                   FROM analytics.mv_timer_day_rollup m2
                  WHERE (m2.person_key = t.emp_id)
                 LIMIT 1) th ON (true))
          WHERE ((t.work_date IS NOT NULL) AND (t.work_date <= ( SELECT today_pht.d
                   FROM today_pht)))) b;
