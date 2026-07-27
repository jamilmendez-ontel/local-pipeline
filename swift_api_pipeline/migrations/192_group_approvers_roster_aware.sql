-- 192: make analytics.group_approvers (migration 136) roster-aware, so the
-- "Approvers" list on a department's scorecard panel shows everyone HR's
-- sheet assigns to approve that department's members, not just whoever has
-- history under the exact literal Swift assigned_approver string.
--
-- Bug: Swift's assigned_approver for a subset of a department's members is
-- sometimes set to an individual's name instead of the shared department
-- queue label -- e.g. Hjalmark Sanchez (carrier_group QPI) files under the
-- "Akira Nakaegawa" queue, not "Daily Report Approvers - Quality and Process
-- Improvement (QPI)". group_approvers matched assigned_approver = p_group
-- exactly, so opening the QPI panel's Approvers list never surfaced Akira,
-- even though he is the one actually approving Hjalmark's reports and is
-- HR's authoritative assignment for him (reference.ref_employee_approvers,
-- migration 182, synced from the same HR roster sheet).
--
-- Fix: keep the existing exact-label history match (unchanged: same rows,
-- same counts, no merge of scorecard metrics -- migration 134 is untouched).
-- ADDITIVELY union in anyone reference.ref_employee_approvers (kind='dr')
-- assigns to approve a member whose OWN carrier_group matches p_group's
-- mapped carrier_group (reference.ref_approver_group), regardless of which
-- literal queue label that member's reports were filed under. Matched by
-- lower(name) so a roster-only addition (approved_count 0, e.g. an approver
-- assigned but not yet exercised) coalesces with their real history count
-- when the same name already appears there.
--
-- No-op for any p_group without a mapped carrier_group (orphan/person-named
-- queues, "(no approver assigned)", groups with carrier_group NULL) -- those
-- keep exactly today's history-only behavior.

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
  SELECT COALESCE(r.approver, h.approver) AS approver, COALESCE(h.approved_count, 0) AS approved_count
  FROM history h
  FULL OUTER JOIN roster r ON r.name_key = h.name_key
  ORDER BY approved_count DESC, approver;
$$;

REVOKE ALL ON FUNCTION analytics.group_approvers(text) FROM PUBLIC;
GRANT EXECUTE ON FUNCTION analytics.group_approvers(text) TO service_role;

UPDATE agent.schema_metadata
SET description = description || ' As of migration 192, additionally unions in anyone reference.ref_employee_approvers (kind=''dr'', HR roster-authoritative) assigns to approve a member of this group''s mapped carrier_group, even if that member''s reports are filed under a different (e.g. person-named) Swift queue label. approved_count is 0 for a roster-only addition with no history under the literal p_group label.'
WHERE schema_name = 'analytics' AND table_name = 'group_approvers' AND column_name IS NULL;

NOTIFY pgrst, 'reload schema';
