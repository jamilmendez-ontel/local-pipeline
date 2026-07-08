-- 163: two display-semantics fixes on analytics.v_daily_report_approvals for
-- the Ontel People Report Review / Daily Reports surfaces (Jamil 2026-07-08).
-- CREATE OR REPLACE contract: column list/order byte-identical to migration 162;
-- v_hr_report_review selects FROM this view, so both changes flow through it
-- with no further DDL.
--
-- 1) employee_name now PREFERS the name written on the daily report itself.
--    Swift daily-report assets are named "FullName_<emp_id>" (e.g.
--    "Jamil Mendez_250901"); the parse of that asset name moves from the
--    2nd to the 1st COALESCE arm, ahead of the roster full_name. Rationale:
--    the roster spells out full legal names ("Prince Raymond Mendiola
--    Tengco") while HR reads reports under the Swift display name ("Prince
--    Tengco"); the app now shows Emp ID in its own column, so the report
--    name is the natural label. Roster full_name and the timer attendance
--    name remain as fallbacks. Measured 2026-07-08: 44,178/44,178 rows have
--    a parseable asset name; 39,551 change spelling vs the roster.
--
-- 2) clock_in_late_minutes now FLOORS seconds to whole minutes (was
--    ::integer, which ROUNDS: a 30m30s late clock-in already read as 31).
--    HR's confirmed grace period is "late only past 30m 59s", so the app
--    flags minutes > 30; with floor, 30:59 -> 30 (not late) and 31:00 -> 31
--    (late), matching the rule exactly. App constant LATE_GRACE_MINUTES = 30
--    (ontel-people lib/hr/domain/late-clock-in.ts).
--
-- Rollback: re-run migration 162.

CREATE OR REPLACE VIEW analytics.v_daily_report_approvals AS
 WITH today_et AS (
         SELECT (now() AT TIME ZONE 'America/New_York'::text)::date AS d
        ),
      today_pht AS (
         SELECT (now() AT TIME ZONE 'Asia/Manila'::text)::date AS d
        )
 SELECT t.emp_id,
    -- employee_name: the report's own "FullName_<emp_id>" asset-name parse wins;
    -- roster full_name, then the timer attendance name, are fallbacks (change #1).
    COALESCE(
        CASE WHEN t.asset_name IS NOT NULL AND t.emp_id IS NOT NULL
                  AND "right"(t.asset_name, length(t.emp_id) + 1) = ('_'::text || t.emp_id)
             THEN "left"(t.asset_name, length(t.asset_name) - length(t.emp_id) - 1)
             ELSE NULL::text END,
        e.full_name,
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
    e.shift_time_in_pht,
    -- clock_in_late_minutes: floor() the seconds (change #2), then wrap to [-720, +720).
    CASE WHEN r.first_clock_in IS NOT NULL
              AND e.shift_time_in_pht ~* '^\s*\d{1,2}(:\d{2})?\s*(AM|PM)\s*$'
         THEN mod(
                floor(extract(epoch FROM (
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
