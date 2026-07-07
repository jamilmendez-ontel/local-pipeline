-- 156: Overlay app-side approvals onto the daily-report serving view so an
-- "Approve in Swift" (single or bulk) flips the report to Approved INSTANTLY for
-- EVERY user, instead of waiting for the next ETL pull from Swift to land.
--
-- Problem: every status the ontel-people app shows (Browse status column, the
-- awaiting-approval queue + its KPIs, the approver scorecard, the pending panel)
-- reads analytics.v_daily_report_approvals, which derives task_status / approved_on
-- from data_staging.stg_daily_reports. That staging row only reflects an approval
-- after the daily-reports pipeline re-pulls the task from Swift (every ~5 min, and
-- longer end-to-end). So a report the app just approved keeps showing "submitted"
-- for the approving user's peers (and after a reload) until the warehouse catches
-- up. The app already persists every successful approve in app_hr.report_approval_log
-- (ok, unique per task_did) -- we just weren't reading it here.
--
-- Fix: LEFT JOIN the log's successful, recent rows and treat a logged approval as
-- authoritative WHEN the warehouse has not caught up yet. Only the 7 status-derived
-- expressions change; the column list/order is byte-identical to migration 143 (the
-- CREATE OR REPLACE contract), so every dependent object (v_daily_report_approver_stats,
-- approval_queue_summary, approver_scorecard, the HR review views, the app, DARA) is
-- unaffected in shape and inherits the corrected status for free.
--
-- Self-cleaning by construction: the log join only diverges from the warehouse
-- during the catch-up window. Once the pipeline pulls the approval, t.approved_on is
-- set and COALESCE prefers it, so the overlay becomes a no-op for that task -- the
-- overridden set shrinks to zero as the warehouse catches up and never accumulates.
-- Bounded to the last 30 days of log rows (the pipeline catches up in minutes, so
-- anything older is certainly already in staging) to keep the join's working set tiny.
--
-- approved_by namespace: the warehouse stores a display name ("Abbie Cariño") while
-- the log stores the approver's login email. During the overlay window we resolve the
-- email to reference.ref_employees.full_name so the scorecard's approver credit stays
-- in the same namespace (falling back to the raw email if unresolved).
--
-- pending_wait_days / approval_latency_days deliberately stay measured on
-- America/New_York, and the PHT visibility cutoff is unchanged (migration 143).
-- Rollback: re-run migration 143.

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
        AND t.assigned_approver IS NULL) AS no_approver_flag
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
            re.carrier_group, re.cluster, re.division, re.sub_division, re.employment_status
           FROM reference.ref_employees re
          WHERE re.emp_id = t.emp_id AND re.effective_date <= COALESCE(t.work_date, CURRENT_DATE)
          ORDER BY re.effective_date DESC LIMIT 1) e ON true
  WHERE t.work_date IS NOT NULL AND t.work_date <= ((SELECT today_pht.d FROM today_pht));
