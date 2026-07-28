-- 196: union the app_hr.hr_member_approver overlay (migration 194) into
-- analytics.group_approvers (roster-primary as of migration 193). This is the
-- display side: a group's scorecard Approvers list also shows anyone assigned
-- IN THE TOOL to approve a member of that group, even if HR's sheet doesn't
-- list them (e.g. Mikaela on the All Project Associates list).
--
-- "Member of p_group" for the overlay = either (a) their carrier_group maps to
-- p_group via reference.ref_approver_group (the 193 rule), or (b) they file
-- reports under p_group as assigned_approver -- (b) is what lets carrier-group-less
-- queues like "All Project Associates (PASs / PCs)" resolve their membership.
-- Manual approvers come back with approved_count 0 (like a roster-only add) and
-- are de-duplicated by lower(name) against the roster/history base.

CREATE OR REPLACE FUNCTION analytics.group_approvers(p_group text)
RETURNS TABLE (approver text, approved_count integer)
LANGUAGE sql
STABLE
SECURITY INVOKER
SET search_path = analytics, reference, public
AS $$
  WITH target AS (
    SELECT carrier_group FROM reference.ref_approver_group WHERE group_label = p_group
  ),
  history AS (
    SELECT approved_by AS approver, lower(approved_by) AS name_key, count(*)::int AS approved_count
    FROM analytics.v_daily_report_approvals
    WHERE task_status = 'approved'
      AND approved_by IS NOT NULL
      AND COALESCE(assigned_approver, '(no approver assigned)') = p_group
    GROUP BY approved_by
  ),
  roster AS (
    SELECT DISTINCT ea.approver_name AS approver, lower(ea.approver_name) AS name_key
    FROM reference.ref_employee_approvers ea
    JOIN reference.ref_employees re ON re.emp_id = ea.emp_id
    CROSS JOIN target t
    WHERE ea.kind = 'dr'
      AND t.carrier_group IS NOT NULL
      AND re.carrier_group = t.carrier_group
  ),
  -- roster-primary base (migration 193): roster when present, else history.
  base AS (
    SELECT r.approver, r.name_key, COALESCE(h.approved_count, 0) AS approved_count
    FROM roster r
    LEFT JOIN history h ON h.name_key = r.name_key
    WHERE EXISTS (SELECT 1 FROM roster)
    UNION ALL
    SELECT h.approver, h.name_key, h.approved_count
    FROM history h
    WHERE NOT EXISTS (SELECT 1 FROM roster)
  ),
  -- members belonging to this group: by mapped carrier_group OR by filing under the queue.
  group_members AS (
    SELECT re.emp_id
    FROM reference.ref_employees re
    CROSS JOIN target t
    WHERE t.carrier_group IS NOT NULL AND re.carrier_group = t.carrier_group
    UNION
    SELECT DISTINCT a.emp_id
    FROM analytics.v_daily_report_approvals a
    WHERE a.emp_id IS NOT NULL
      AND COALESCE(a.assigned_approver, '(no approver assigned)') = p_group
  ),
  manual AS (
    SELECT DISTINCT o.approver_name AS approver, lower(o.approver_name) AS name_key
    FROM app_hr.hr_member_approver o
    JOIN group_members gm ON gm.emp_id = o.member_emp_id
  )
  SELECT approver, approved_count FROM base
  UNION ALL
  SELECT m.approver, 0 FROM manual m
  WHERE m.name_key NOT IN (SELECT name_key FROM base)
  ORDER BY approved_count DESC, approver;
$$;

REVOKE ALL ON FUNCTION analytics.group_approvers(text) FROM PUBLIC;
GRANT EXECUTE ON FUNCTION analytics.group_approvers(text) TO service_role;

UPDATE agent.schema_metadata
SET description = description || ' As of migration 196, also unions in approvers assigned via the app_hr.hr_member_approver in-tool overlay for members of the group (by mapped carrier_group or by who files under the queue), with approved_count 0.'
WHERE schema_name = 'analytics' AND table_name = 'group_approvers' AND column_name IS NULL;

NOTIFY pgrst, 'reload schema';
