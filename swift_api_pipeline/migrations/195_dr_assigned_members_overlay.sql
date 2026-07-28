-- 195: union the app_hr.hr_member_approver overlay (migration 194) into
-- analytics.dr_assigned_members_for_email (migration 182). This is the
-- capability/visibility side: the members a given app user is recognized to
-- approve = HR's roster assignments (unchanged) PLUS any manual tool overrides
-- assigning them to that member. Backs the Home "Team members" tile and the
-- /directory?approver= drill-down (getTeamMemberIds -> getAssignedMemberIds).
--
-- Overlay match mirrors the roster match: by lower(approver_email) = p_email, or
-- by approver_emp_id resolved (alias-aware via ref_employee_emails) to the same
-- person as p_email. Directory join keeps only directory-resolvable members, so a
-- stray override to an unknown emp_id can't leak a phantom row.

CREATE OR REPLACE FUNCTION analytics.dr_assigned_members_for_email(p_email text)
RETURNS SETOF text
LANGUAGE sql
STABLE
SET search_path = analytics
AS $$
  WITH me AS (
    SELECT (SELECT ea.emp_id
            FROM reference.ref_employee_emails ea
            WHERE ea.email = lower(p_email)
            ORDER BY ea.last_seen DESC
            LIMIT 1) AS emp_id
  )
  -- HR roster (sheet) assignments — unchanged from migration 182
  SELECT DISTINCT a.emp_id
  FROM me, reference.ref_employee_approvers a
  JOIN analytics.v_employee_directory d ON d.emp_id = a.emp_id
  WHERE a.kind = 'dr'
    AND (
      (a.approver_emp_id IS NOT NULL AND a.approver_emp_id = me.emp_id)
      OR lower(a.approver_email) = lower(p_email)
    )
  UNION
  -- manual tool overrides (app_hr overlay, migration 194)
  SELECT DISTINCT o.member_emp_id
  FROM me, app_hr.hr_member_approver o
  JOIN analytics.v_employee_directory d ON d.emp_id = o.member_emp_id
  WHERE (
      lower(o.approver_email) = lower(p_email)
      OR (o.approver_emp_id IS NOT NULL AND o.approver_emp_id = me.emp_id)
    );
$$;

REVOKE ALL ON FUNCTION analytics.dr_assigned_members_for_email(text) FROM PUBLIC;
GRANT EXECUTE ON FUNCTION analytics.dr_assigned_members_for_email(text) TO service_role;

UPDATE agent.schema_metadata
SET description = description || ' As of migration 195, additionally includes members assigned via the app_hr.hr_member_approver in-tool overlay (matched by approver email or resolved emp_id), so tool-only approver assignments count toward "members you approve" without editing the HR sheet.'
WHERE schema_name = 'analytics' AND table_name = 'dr_assigned_members_for_email' AND column_name IS NULL;

NOTIFY pgrst, 'reload schema';
