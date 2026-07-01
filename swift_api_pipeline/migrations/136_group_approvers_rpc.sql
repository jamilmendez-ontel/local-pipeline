-- 136: analytics.group_approvers(p_group) — the individuals who approve for one
-- approver group, with how many they've approved (all-time). Powers the "Approvers"
-- line in the scorecard pending panel. p_group matches COALESCE(assigned_approver,
-- '(no approver assigned)') so the no-approver sentinel works with the same key.

CREATE OR REPLACE FUNCTION analytics.group_approvers(p_group text)
RETURNS TABLE (approver text, approved_count integer)
LANGUAGE sql
STABLE
SECURITY INVOKER
SET search_path = analytics, public
AS $$
  SELECT approved_by AS approver, count(*)::int AS approved_count
  FROM analytics.v_daily_report_approvals
  WHERE task_status = 'approved'
    AND approved_by IS NOT NULL
    AND COALESCE(assigned_approver, '(no approver assigned)') = p_group
  GROUP BY approved_by
  ORDER BY count(*) DESC, approved_by;
$$;

REVOKE ALL ON FUNCTION analytics.group_approvers(text) FROM PUBLIC;
GRANT EXECUTE ON FUNCTION analytics.group_approvers(text) TO service_role;

INSERT INTO agent.schema_metadata (schema_name, table_name, description, business_context, related_tables)
VALUES
  ('analytics','group_approvers',
   'Function(p_group): individuals who approve daily reports for one approver group, with approved counts, all-time. p_group matches COALESCE(assigned_approver, ''(no approver assigned)'').',
   'Powers the Approvers line in the HR scorecard pending panel — shows who actually approves for a group (groups often have several approvers).',
   ARRAY['analytics.v_daily_report_approvals'])
ON CONFLICT DO NOTHING;
