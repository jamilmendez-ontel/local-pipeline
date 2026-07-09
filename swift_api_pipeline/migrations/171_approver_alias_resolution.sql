-- 171: resolve approver identity through the email alias map (167/169), so an
-- approver whose email changes keeps (a) their name on past approvals in
-- v_daily_report_approvals and (b) their owned queues / team members in the
-- approver_groups_for_email / approved_members_for_email lookups.
--
-- Three changes, all alias-first with the old email match kept as fallback:
--   1. v_daily_report_approvals `appr` lateral (approver name for app-approved
--      rows): match ref_employees by alias-resolved emp_id OR email.
--   2/3. approver_groups_for_email + approved_members_for_email `me` lookup:
--      directory row by alias-resolved emp_id OR email. ALSO fixes a latent
--      self-collision bug: the ambiguity guard excluded self BY EMAIL, so
--      after an email change the user's own directory row would count as a
--      name collision with themselves and the function would refuse
--      everything; self-exclusion is now by emp_id.
-- View def below = live pg_get_viewdef 2026-07-09 (post-163) with only the
-- appr lateral changed.

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
        END AS clock_in_late_minutes
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
                 LIMIT 1))
             OR lower(re2.email) = lower(la.approver_email)
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
  WHERE t.work_date IS NOT NULL AND t.work_date <= (( SELECT today_pht.d
           FROM today_pht));

CREATE OR REPLACE FUNCTION analytics.approver_groups_for_email(p_email text)
RETURNS SETOF text
LANGUAGE sql
STABLE
SET search_path = analytics
AS $$
  WITH me AS (
    SELECT
      emp_id,
      lower(coalesce(nickname, '')   || ' ' || coalesce(last_name, '')) AS nick_key,
      lower(coalesce(first_name, '') || ' ' || coalesce(last_name, '')) AS first_key
    FROM analytics.v_employee_directory
    WHERE emp_id = (SELECT ea.emp_id FROM reference.ref_employee_emails ea
                    WHERE ea.email = lower(p_email)
                    ORDER BY ea.last_seen DESC LIMIT 1)
       OR lower(email) = lower(p_email)
    LIMIT 1
  ),
  -- Count OTHER active employees sharing either name key (self excluded by
  -- emp_id, not email, so the guard survives the user's own email change).
  ambiguous AS (
    SELECT count(*) AS collisions
    FROM analytics.v_employee_directory d, me
    WHERE d.is_active
      AND d.emp_id <> me.emp_id
      AND (
           lower(coalesce(d.nickname, '')   || ' ' || coalesce(d.last_name, '')) = me.nick_key
        OR lower(coalesce(d.first_name, '') || ' ' || coalesce(d.last_name, '')) = me.first_key
      )
  )
  SELECT DISTINCT a.assigned_approver
  FROM me, ambiguous, analytics.v_daily_report_approvals a
  WHERE ambiguous.collisions = 0
    AND a.approved_by IS NOT NULL
    AND a.assigned_approver IS NOT NULL
    AND a.work_date >= ((now() AT TIME ZONE 'Asia/Manila')::date - 180)
    AND (lower(a.approved_by) = me.nick_key OR lower(a.approved_by) = me.first_key);
$$;

CREATE OR REPLACE FUNCTION analytics.approved_members_for_email(p_email text)
RETURNS SETOF text
LANGUAGE sql
STABLE
SET search_path = analytics
AS $$
  WITH me AS (
    SELECT
      emp_id,
      lower(coalesce(nickname, '')   || ' ' || coalesce(last_name, '')) AS nick_key,
      lower(coalesce(first_name, '') || ' ' || coalesce(last_name, '')) AS first_key
    FROM analytics.v_employee_directory
    WHERE emp_id = (SELECT ea.emp_id FROM reference.ref_employee_emails ea
                    WHERE ea.email = lower(p_email)
                    ORDER BY ea.last_seen DESC LIMIT 1)
       OR lower(email) = lower(p_email)
    LIMIT 1
  ),
  ambiguous AS (
    SELECT count(*) AS collisions
    FROM analytics.v_employee_directory d, me
    WHERE d.is_active
      AND d.emp_id <> me.emp_id
      AND (
           lower(coalesce(d.nickname, '')   || ' ' || coalesce(d.last_name, '')) = me.nick_key
        OR lower(coalesce(d.first_name, '') || ' ' || coalesce(d.last_name, '')) = me.first_key
      )
  )
  SELECT DISTINCT a.emp_id
  FROM me, ambiguous, analytics.v_daily_report_approvals a
  JOIN analytics.v_employee_directory d2 ON d2.emp_id = a.emp_id
  WHERE ambiguous.collisions = 0
    AND a.approved_by IS NOT NULL
    AND a.emp_id IS NOT NULL
    AND a.work_date >= ((now() AT TIME ZONE 'Asia/Manila')::date - 180)
    AND (lower(a.approved_by) = me.nick_key OR lower(a.approved_by) = me.first_key);
$$;

REVOKE ALL ON FUNCTION analytics.approver_groups_for_email(text) FROM PUBLIC;
GRANT EXECUTE ON FUNCTION analytics.approver_groups_for_email(text) TO service_role;
REVOKE ALL ON FUNCTION analytics.approved_members_for_email(text) FROM PUBLIC;
GRANT EXECUTE ON FUNCTION analytics.approved_members_for_email(text) TO service_role;

UPDATE agent.schema_metadata
SET description = description || ' Identity lookups resolve the input email through reference.ref_employee_emails (alias-first, email fallback; self-exclusion by emp_id) as of migration 171, so an approver''s email change does not break queue/team attribution.'
WHERE schema_name = 'analytics'
  AND table_name IN ('approver_groups_for_email','approved_members_for_email')
  AND column_name IS NULL;
