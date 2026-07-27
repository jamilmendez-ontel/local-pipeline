-- 193: analytics.group_approvers (136, additively unioned in 192) goes
-- roster-PRIMARY: when HR's roster (reference.ref_employee_approvers,
-- migration 182) has any kind='dr' assignment for a department's members,
-- it becomes the sole source for that department's Approvers list --
-- dropping any historical approver whose name isn't in the current roster,
-- not just adding missing ones.
--
-- 192 only unioned roster names IN; a lead who used to approve a group's
-- reports but has since been reassigned (per the roster) still lingered in
-- the list forever, because the exact-label approval history never expires.
-- Per-department roster coverage is near-complete (QPI 10/10, CG1 186/192,
-- CG2 26/26, CG3 30/32, Accounting/DA/HR/Research/Tools&Auto all 100%,
-- verified 2026-07-27), so it's safe to trust it fully once present rather
-- than blend it with stale history.
--
-- Verified for QPI: roster-only names are {Czarina Sanchez, Glaiza Figueroa,
-- Akira Nakaegawa, Kris Kerr}. History also carried Dee Bernabe, Merjien
-- Lara, Lenard Garcia (old/backup approvals, no current assignment) --
-- those now drop from the QPI list.
--
-- Unchanged: any p_group with no carrier_group mapping (orphan/person-named
-- queues, "(no approver assigned)") or a mapped carrier_group with zero
-- roster rows falls back to today's pure-history behavior -- never shows an
-- empty list just because roster sync is momentarily incomplete.

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
  )
  SELECT r.approver, COALESCE(h.approved_count, 0) AS approved_count
  FROM roster r
  LEFT JOIN history h ON h.name_key = r.name_key
  WHERE EXISTS (SELECT 1 FROM roster)
  UNION ALL
  SELECT h.approver, h.approved_count
  FROM history h
  WHERE NOT EXISTS (SELECT 1 FROM roster)
  ORDER BY approved_count DESC, approver;
$$;

REVOKE ALL ON FUNCTION analytics.group_approvers(text) FROM PUBLIC;
GRANT EXECUTE ON FUNCTION analytics.group_approvers(text) TO service_role;

UPDATE agent.schema_metadata
SET description = description || ' As of migration 193, once reference.ref_employee_approvers (HR roster, kind=''dr'') has ANY assignment for the group''s mapped carrier_group, the roster becomes the sole source of the approver list (dropping stale approvers no longer assigned, in addition to adding new ones) -- history is used only as a fallback when the roster has no rows for that department.'
WHERE schema_name = 'analytics' AND table_name = 'group_approvers' AND column_name IS NULL;

NOTIFY pgrst, 'reload schema';
