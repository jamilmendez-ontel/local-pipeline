-- 146: single source of truth for HR Report Review + HR Dashboard.
-- Grain: one row per daily report; (emp_id, work_date) verified unique.
-- Definitions (spec 2026-07-04): late = submitted > 48h after time-in;
-- missing = pending/in_progress + work evidence (clock-in OR timer day) +
-- 48h passed; no evidence = never flagged (weekends/holidays/leave stay quiet).
--
-- Adjustments vs the task brief's Step 1 sketch (verified against live schema
-- before applying; see task-2-report.md for full detail):
--   1. `first_clock_in` does NOT live on data_staging.stg_daily_reports (brief's
--      `t.first_clock_in`) -- it lives on analytics.mv_daily_report_task_rollup
--      (already listed as a Consumes: dependency in the brief). Added an explicit
--      LEFT JOIN to it (aliased r, 1:1 on task_did, verified via distinct count)
--      and read first_clock_in from there.
--   2. Dropped the brief's separate LEFT JOIN LATERAL to reference.ref_employees
--      for email/position: analytics.v_daily_report_approvals (aliased b) already
--      exposes b.email and b."position" via the identical effective-dated lateral
--      join, so re-deriving them here would just be a second, redundant copy of
--      the same logic (and risked drifting from it). Used b.email / b."position"
--      directly; tm/th now key off b.email instead of a locally re-derived e.email.
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
                                                           AS coverage_pct
FROM analytics.v_daily_report_approvals b
JOIN data_staging.stg_daily_reports t USING (task_did)
LEFT JOIN analytics.mv_daily_report_task_rollup r ON r.task_did = t.task_did
LEFT JOIN analytics.mv_timer_day_rollup tm
  ON tm.user_email = b.email AND tm.work_day = b.work_date
LEFT JOIN LATERAL (
  SELECT m2.user_email FROM analytics.mv_timer_day_rollup m2
  WHERE m2.user_email = b.email LIMIT 1
) th ON true;

-- Semantic-layer metadata (DATABASE_ARCHITECTURE standard). agent.schema_metadata
-- columns/conflict shape per migration 144/145 (NOT the schema_name/object_name/
-- object_type template originally sketched in the task brief: actual table has
-- table_name, description, business_context, related_tables and a unique
-- constraint on (schema_name, table_name, column_name); we follow ON CONFLICT
-- DO NOTHING as in 145).
INSERT INTO agent.schema_metadata (schema_name, table_name, description, business_context, related_tables)
VALUES
  ('analytics', 'v_hr_report_review',
   'HR Report Review serving view: one row per daily report with filing lag vs 48h deadline (is_late_filing), work-evidence based is_missing_report, is_matured, timer union hours + open_timer_count + has_timer_history, stated-vs-timed variance_hours and coverage_pct. Position/email via the effective-dated ref_employees lookup already embedded in v_daily_report_approvals; first_clock_in sourced from mv_daily_report_task_rollup.',
   'Single source of truth for the /hr Report Review + HR Dashboard pages; both RPCs read it.',
   ARRAY['analytics.v_daily_report_approvals', 'analytics.mv_timer_day_rollup', 'data_staging.stg_daily_reports', 'reference.ref_employees'])
ON CONFLICT DO NOTHING;
