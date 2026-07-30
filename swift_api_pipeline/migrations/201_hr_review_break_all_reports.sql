-- 201: apply the 1-hour unpaid break to ALL daily reports (Jamil 2026-07-30).
-- Supersedes the migration-190 rule that only netted the break out for reports
-- of 5h or more. The real policy: EVERY daily report includes a 1-hour unpaid
-- break, regardless of length. So the three break expressions in
-- v_hr_report_review change from
--     CASE WHEN b.total_hours >= 5 THEN b.total_hours - 1 ELSE b.total_hours END
-- to
--     GREATEST(b.total_hours - 1, 0)
-- The GREATEST floor keeps net hours at 0 (never negative) for reports at or
-- below 1h -- notably the ~706 zero-hour (empty/pending) reports, which already
-- netted to 0 and are already excluded from variance by the coverage guard.
-- Deducted once per report (per row here = per daily report), unchanged.
--
-- Scope: VIEW ONLY. Unlike migration 190 (which dropped hr_review_page to add a
-- column), this changes only the three break EXPRESSIONS; the column list,
-- names, types, and order are identical, so a bare CREATE OR REPLACE VIEW is
-- legal even though hr_review_page/count return SETOF this view. We must NOT
-- drop/recreate the RPCs -- that would revert migrations 198 (variance % range)
-- and 199 (clock-in flags + sorts). The RPCs read stated_hours_net/coverage_pct
-- FROM this view and do not re-derive the break, so DR Monitoring, the flag
-- counts, the variance KPI tile, the variance % filter, the variance sort, and
-- the Hours Variance dashboard all inherit the new rule automatically.
--
-- stated_hours (raw b.total_hours) is UNCHANGED -- the report detail drawer
-- reads it. Base text: migration 190's committed view definition.
--
-- Rollback: re-create the view with the migration-190 expressions
-- (CASE WHEN b.total_hours >= 5 THEN b.total_hours - 1 ELSE b.total_hours END)
-- in all three places.

CREATE OR REPLACE VIEW analytics.v_hr_report_review AS
 SELECT b.emp_id,
    b.employee_name,
    b.email,
    b."position",
    b.carrier_group,
    b.division,
    b.work_date,
    b.task_did,
    b.task_status,
    b.submitted_on_et,
    b.approved_on_et,
    b.clock_in_et,
    b.approval_latency_days,
    b.total_hours AS stated_hours,
    r.first_clock_in IS NOT NULL AS has_time_in,
    round(EXTRACT(epoch FROM t.submitted_on - r.first_clock_in) / 3600.0, 1) AS filing_lag_hours,
    r.first_clock_in + '49:00:00'::interval AS deadline_at,
    t.submitted_on IS NOT NULL AND r.first_clock_in IS NOT NULL AND t.submitted_on >= (r.first_clock_in + '49:00:00'::interval) AS is_late_filing,
    COALESCE(r.first_clock_in, tm.first_start) AS evidence_at,
    (b.task_status = ANY (ARRAY['pending'::text, 'in_progress'::text])) AND COALESCE(r.first_clock_in, tm.first_start) IS NOT NULL AND now() >= (COALESCE(r.first_clock_in, tm.first_start) + '49:00:00'::interval) AS is_missing_report,
    COALESCE(r.first_clock_in, tm.first_start) IS NOT NULL AND now() >= (COALESCE(r.first_clock_in, tm.first_start) + '49:00:00'::interval) AS is_matured,
        CASE
            WHEN tm.person_key IS NOT NULL THEN round(tm.union_min / 60.0, 1)
            ELSE NULL::numeric
        END AS timed_hours,
    COALESCE(tm.open_count, 0::bigint) AS open_timer_count,
    COALESCE(tm.entry_count, 0::bigint) AS timer_entry_count,
    th.person_key IS NOT NULL AS has_timer_history,
        CASE
            WHEN tm.person_key IS NOT NULL AND COALESCE(tm.open_count, 0::bigint) = 0 AND b.total_hours IS NOT NULL
              THEN round(GREATEST(b.total_hours - 1::numeric, 0::numeric) - tm.union_min / 60.0, 1)
            ELSE NULL::numeric
        END AS variance_hours,
        CASE
            WHEN tm.person_key IS NOT NULL AND COALESCE(tm.open_count, 0::bigint) = 0 AND b.total_hours IS NOT NULL
              AND GREATEST(b.total_hours - 1::numeric, 0::numeric) > 0::numeric
              THEN round(100.0 * tm.union_min / 60.0 / GREATEST(b.total_hours - 1::numeric, 0::numeric), 0)
            ELSE NULL::numeric
        END AS coverage_pct,
    b.shift_time_in_pht,
    b.clock_in_late_minutes,
    EXTRACT(dow FROM b.work_date)::smallint AS work_dow,
    GREATEST(b.total_hours - 1::numeric, 0::numeric) AS stated_hours_net
   FROM analytics.v_daily_report_approvals b
     JOIN data_staging.stg_daily_reports t USING (task_did)
     LEFT JOIN analytics.mv_daily_report_task_rollup r ON r.task_did = t.task_did
     LEFT JOIN analytics.mv_timer_day_rollup tm ON tm.person_key = b.emp_id AND tm.work_day = b.work_date
     LEFT JOIN LATERAL ( SELECT m2.person_key
           FROM analytics.mv_timer_day_rollup m2
          WHERE m2.person_key = b.emp_id
         LIMIT 1) th ON true;

NOTIFY pgrst, 'reload schema';
