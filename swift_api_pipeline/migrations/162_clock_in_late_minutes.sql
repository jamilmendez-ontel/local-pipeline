-- 162: expose scheduled shift start + late-clock-in minutes on the daily-report
-- serving views, for the Ontel People late clock-in badges (spec
-- ontel-people/docs/superpowers/specs/2026-07-08-late-clock-in-badges-design.md).
-- NOTE: applied to prod 2026-07-08 under the supabase_migrations name
-- "157_clock_in_late_minutes" (numbering collision with the already-existing
-- 157_approver_scorecard_payroll_deadline.sql caught after apply; file renamed
-- to 162, DB history row left as-is).
--
-- Two columns APPENDED to analytics.v_daily_report_approvals (CREATE OR REPLACE
-- contract: existing column list/order byte-identical to migration 156, so every
-- dependent object is unaffected in shape):
--   shift_time_in_pht      text  -- roster shift start, e.g. '9:00 PM' (migration
--                                -- 153 field), from the SAME effective-dated
--                                -- ref_employees lateral the view already has.
--   clock_in_late_minutes  int   -- (first_clock_in AT TIME ZONE 'Asia/Manila')::time
--                                -- minus shift_time_in_pht::time, wrapped to
--                                -- [-720, +720). Negative = early. NULL when there
--                                -- is no clock-in or no parseable shift time.
--
-- Timezone rule: PHT is computed with AT TIME ZONE 'Asia/Manila' on the raw
-- timestamptz. Deriving it as clock_in_et + 12h would silently go off by one
-- hour every winter (PHT is fixed UTC+8; ET flips UTC-4/UTC-5).
--
-- The wrap to [-720, +720) handles overnight shifts: a 9:00 PM PHT shift with a
-- 00:30 AM clock-in reads +210 (late), an 8:40 PM clock-in reads -20 (early).
-- The interim lateness threshold (15 min grace) lives in the app, NOT here, so
-- HR's confirmed grace period is a one-line app change.
--
-- Then v_hr_report_review (migration 146) is recreated with both columns
-- appended as pass-throughs.
--
-- Validated 2026-07-08 against a 14-day sample (846 rows): 77% on-time/early,
-- 17% late 1-15m, 3% late 16m-4h, 22 rows outside +/-4h (off-schedule days).
-- Rollback: re-run migrations 156 + 146.

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
                  AND "right"(t.asset_name, length(t.emp_id) + 1) = ('_'::text || t.emp_id)
             THEN "left"(t.asset_name, length(t.asset_name) - length(t.emp_id) - 1)
             ELSE NULL::text END,
        r.attendance_user_name
    ) AS employee_name,
    e.nickname, e.email, e."position", e.carrier, e.carrier_group, e.cluster,
    e.division, e.sub_division, e.employment_status,
    t.work_date, t.task_did,
    -- task_status: a logged app approval reads as 'approved' until the warehouse agrees.
    CASE WHEN t.task_status = 'approved'::text THEN t.task_status
         WHEN la.task_did IS NOT NULL          THEN 'approved'::text
         ELSE t.task_status END AS task_status,
    t.asset_name, t.milestone,
    COALESCE(r.req_count, 0::bigint) AS req_count,
    r.total_hours,
    (r.first_clock_in AT TIME ZONE 'America/New_York'::text) AS clock_in_et,
    t.assigned_approver,
    (t.submitted_on AT TIME ZONE 'America/New_York'::text) AS submitted_on_et,
    -- approved_on_et: warehouse time if present, else the app approval time.
    (COALESCE(t.approved_on, la.approved_at) AT TIME ZONE 'America/New_York'::text) AS approved_on_et,
    -- approved_by: warehouse name if present, else the app approver (email resolved to name).
    COALESCE(t.approved_by,
             CASE WHEN la.task_did IS NOT NULL THEN COALESCE(appr.full_name, la.approver_email) END) AS approved_by,
    -- is_awaiting_approval: false once approved either in the warehouse OR in the app log.
    (t.task_status = 'submitted'::text AND t.approved_on IS NULL AND la.task_did IS NULL) AS is_awaiting_approval,
        CASE WHEN t.task_status = 'submitted'::text AND t.approved_on IS NULL AND la.task_did IS NULL
             THEN ((SELECT today_et.d FROM today_et)) - (t.submitted_on AT TIME ZONE 'America/New_York'::text)::date
             ELSE NULL::integer END AS pending_wait_days,
    -- approval_latency_days: uses the effective approval time so app-approved rows are credited too.
        CASE WHEN COALESCE(t.approved_on, la.approved_at) IS NOT NULL AND t.submitted_on IS NOT NULL
             THEN (COALESCE(t.approved_on, la.approved_at) AT TIME ZONE 'America/New_York'::text)::date
                  - (t.submitted_on AT TIME ZONE 'America/New_York'::text)::date
             ELSE NULL::integer END AS approval_latency_days,
    (t.task_status = 'submitted'::text AND t.approved_on IS NULL AND la.task_did IS NULL
        AND t.assigned_approver IS NULL) AS no_approver_flag,
    -- ==== appended by migration 157 (keep at the END of the select list) ====
    e.shift_time_in_pht,
    CASE WHEN r.first_clock_in IS NOT NULL
              AND e.shift_time_in_pht ~* '^\s*\d{1,2}(:\d{2})?\s*(AM|PM)\s*$'
         THEN mod(
                (extract(epoch FROM (
                    (r.first_clock_in AT TIME ZONE 'Asia/Manila'::text)::time
                    - e.shift_time_in_pht::time
                )) / 60)::integer + 2160, 1440) - 720
         ELSE NULL::integer END AS clock_in_late_minutes
   FROM data_staging.stg_daily_reports t
     LEFT JOIN analytics.mv_daily_report_task_rollup r ON t.task_did = r.task_did
     -- Recent successful app-side approvals (idempotent: <=1 ok row per task_did).
     LEFT JOIN LATERAL (
         SELECT l.task_did, l.approver_email, l.approved_at
         FROM app_hr.report_approval_log l
         WHERE l.task_did = t.task_did AND l.ok
           AND l.approved_at >= (now() - interval '30 days')
         LIMIT 1
     ) la ON true
     -- Resolve the app approver's email to their roster name (scorecard namespace).
     LEFT JOIN LATERAL (
         SELECT re2.full_name
         FROM reference.ref_employees re2
         WHERE re2.email = la.approver_email
         ORDER BY re2.effective_date DESC
         LIMIT 1
     ) appr ON la.task_did IS NOT NULL
     LEFT JOIN LATERAL ( SELECT re.full_name, re.nickname, re.email, re."position", re.carrier,
            re.carrier_group, re.cluster, re.division, re.sub_division, re.employment_status,
            re.shift_time_in_pht
           FROM reference.ref_employees re
          WHERE re.emp_id = t.emp_id AND re.effective_date <= COALESCE(t.work_date, CURRENT_DATE)
          ORDER BY re.effective_date DESC LIMIT 1) e ON true
  WHERE t.work_date IS NOT NULL AND t.work_date <= ((SELECT today_pht.d FROM today_pht));

-- v_hr_report_review: identical to migration 146 with the two new columns
-- appended as pass-throughs.
CREATE OR REPLACE VIEW analytics.v_hr_report_review AS
SELECT
  b.emp_id,
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
  b.total_hours                                            AS stated_hours,
  (r.first_clock_in IS NOT NULL)                           AS has_time_in,
  round((EXTRACT(epoch FROM t.submitted_on - r.first_clock_in) / 3600.0)::numeric, 1)
                                                           AS filing_lag_hours,
  (r.first_clock_in + interval '48 hours')                 AS deadline_at,
  (t.submitted_on IS NOT NULL AND r.first_clock_in IS NOT NULL
   AND t.submitted_on > r.first_clock_in + interval '48 hours')
                                                           AS is_late_filing,
  COALESCE(r.first_clock_in, tm.first_start)               AS evidence_at,
  (b.task_status IN ('pending', 'in_progress')
   AND COALESCE(r.first_clock_in, tm.first_start) IS NOT NULL
   AND now() > COALESCE(r.first_clock_in, tm.first_start) + interval '48 hours')
                                                           AS is_missing_report,
  (COALESCE(r.first_clock_in, tm.first_start) IS NOT NULL
   AND now() > COALESCE(r.first_clock_in, tm.first_start) + interval '48 hours')
                                                           AS is_matured,
  CASE WHEN tm.user_email IS NOT NULL
       THEN round((tm.union_min / 60.0)::numeric, 1) END   AS timed_hours,
  COALESCE(tm.open_count, 0)                               AS open_timer_count,
  COALESCE(tm.entry_count, 0)                              AS timer_entry_count,
  (th.user_email IS NOT NULL)                              AS has_timer_history,
  CASE WHEN tm.user_email IS NOT NULL AND COALESCE(tm.open_count, 0) = 0
            AND b.total_hours IS NOT NULL
       THEN round((b.total_hours - tm.union_min / 60.0)::numeric, 1) END
                                                           AS variance_hours,
  CASE WHEN tm.user_email IS NOT NULL AND COALESCE(tm.open_count, 0) = 0
            AND b.total_hours IS NOT NULL AND b.total_hours > 0
       THEN round((100.0 * tm.union_min / 60.0 / b.total_hours)::numeric, 0) END
                                                           AS coverage_pct,
  -- ==== appended by migration 157 ====
  b.shift_time_in_pht,
  b.clock_in_late_minutes
FROM analytics.v_daily_report_approvals b
JOIN data_staging.stg_daily_reports t USING (task_did)
LEFT JOIN analytics.mv_daily_report_task_rollup r ON r.task_did = t.task_did
LEFT JOIN analytics.mv_timer_day_rollup tm
  ON tm.user_email = b.email AND tm.work_day = b.work_date
LEFT JOIN LATERAL (
  SELECT m2.user_email FROM analytics.mv_timer_day_rollup m2
  WHERE m2.user_email = b.email LIMIT 1
) th ON true;
